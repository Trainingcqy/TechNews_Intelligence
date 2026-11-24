"""Tests for picking the article out of Jina Reader markdown."""

from __future__ import annotations

import pytest

from ingest.extract import extract_article, is_paywalled

TITLE = "Example Labs ships Model X for long documents"
P1 = ("Example Labs released Model X on Tuesday, a language model that reads documents of up to two million words "
      "and answers questions about them in a few seconds, the company said in a statement.")
P2 = ("The company said the model was trained on licensed books, court filings and scientific papers, and that "
      "early customers include three law firms and a hospital network in Ohio that tested it for six months.")
P3 = ("Pricing starts at four dollars per million words read, and the model will be available to developers through "
      "the company's interface next month, with a version for phones planned for early next year.")
ARTICLE = f"{P1}\n\n{P2}\n\n{P3}"
NAV = "Skip to main content\n\n* Products\n* Research\n* Company\n* Careers\n\nSign in"
# Prose after the article that outweighs the cost of a section label before it.
TEASERS = ("A startup in Berlin raised forty million dollars to build robots that sort recycling at municipal plants, "
           "according to people familiar with the deal and filings seen by reporters this week.\n\n"
           "Regulators in Brussels opened an inquiry into how three cloud providers price storage for public agencies, "
           "the commission said, adding that the review would take at least nine months to complete.\n\n"
           "A chip designer in Austin said its newest accelerator uses half the power of the previous generation, "
           "and that two large hosting companies have agreed to test it in their data centers next spring.\n\n"
           "Shares of a satellite operator rose after it won a contract to provide internet service to rural schools, "
           "a deal that analysts said could be worth several hundred million dollars over the next decade.")
FOOTER = "© 2026 Example Labs. All rights reserved.\n\n* Privacy\n* Terms"


def page(*parts: str) -> str:
    return "\n\n".join(parts)


def test_reader_output_without_page_furniture_is_returned_whole():
    assert extract_article(ARTICLE, TITLE) == ARTICLE


def test_article_is_cut_from_a_whole_page():
    content = page(NAV, f"# {TITLE}", "November 20, 2026", ARTICLE, "## Related", TEASERS, FOOTER)
    assert extract_article(content, TITLE) == ARTICLE


def test_long_stretch_without_text_ends_the_article_after_the_title():
    furniture = "\n\n".join(["Company", "About us", "Careers", "Press room", "Contact", "Help center", "Status",
                             "Developers", "Partners", "Events", "Podcasts", "Store", "Gift cards", "Jobs", "Legal",
                             "Accessibility"])
    content = page(f"# {TITLE}", ARTICLE, furniture, TEASERS)
    assert extract_article(content, TITLE) == ARTICLE


def test_title_heading_matches_the_part_of_the_title_without_the_site_name():
    promo = "Try our new assistant today and see what it can do for your whole team."
    content = page(promo, NAV, f"# {TITLE}", ARTICLE)
    assert extract_article(content, f"{TITLE} | The Example Engineering Blog") == ARTICLE


def test_title_heading_after_much_prose_is_a_related_link():
    content = page(ARTICLE, "## More from Example Labs", f"### {TITLE}", "### Example Labs opens an office in Tokyo")
    assert extract_article(content, TITLE) == ARTICLE


def test_single_emoji_label_after_the_article_is_not_a_list_of_statements():
    content = page(f"# {TITLE}", ARTICLE, "⚡ Top stories this week", FOOTER)
    assert extract_article(content, TITLE) == ARTICLE


def test_standfirst_next_to_the_first_paragraph_is_kept():
    standfirst = "Model X reads long documents and answers questions about them in seconds"
    content = page(NAV, f"# {TITLE}", standfirst, ARTICLE)
    assert extract_article(content, TITLE) == page(standfirst, ARTICLE)


def test_boilerplate_phrase_inside_a_paragraph_is_article_text():
    lead = ("Example Labs said on Tuesday that it would pause the changes to its terms of service for business "
            "customers after complaints from developers who said the new rules on data use were unclear.")
    content = page(NAV, f"# {TITLE}", lead, P2)
    assert extract_article(content, TITLE) == page(lead, P2)


def test_cookie_notice_addressed_to_the_visitor_is_dropped():
    notice = ("We use cookies to give you a better experience and to measure traffic on our site. By clicking accept "
              "you agree to the storing of cookies on your device.")
    about_cookies = ("Google said Chrome will keep supporting third-party cookies, and regulators said that people "
                     "using the browser should get a clear and simple choice over how advertisers track them.")
    content = page(notice, about_cookies, P2)
    assert extract_article(content, None) == page(about_cookies, P2)


def test_row_of_repeated_link_labels_is_not_prose():
    sites = ("Facebook", "X", "LinkedIn", "Reddit", "Email", "Threads", "Bluesky", "Mastodon", "WhatsApp", "Telegram",
             "Pocket", "Flipboard")
    share = " ".join(f"Share on {site}" for site in sites)
    content = page(P1, NAV, share)
    assert extract_article(content, None) == P1


def test_new_window_labels_do_not_turn_menu_items_into_statements():
    names = ("ChatGPT", "Sora", "Codex", "API Platform", "Docs")
    menu = "\n".join(f"* {name}(opens in a new window)" for name in names)
    content = page(f"# {TITLE}", ARTICLE, menu)
    assert extract_article(content, TITLE) == ARTICLE


def test_short_list_right_after_the_lead_keeps_the_lead():
    lead = "Example Labs released Model X today with these features:"
    items = "\n".join(f"* {feature}" for feature in ("Faster", "Cheaper", "Smaller", "Safer", "Private", "Offline",
                                                    "Multilingual", "Open"))
    content = page(lead, items, ARTICLE)
    assert extract_article(content, None) == content


def test_invisible_characters_do_not_hide_the_end_of_a_sentence():
    # Without the colon read as the end of a sentence, the table of contents would be the only prose on the page.
    features = ("## Key features of the app:\u200b\n\n* 🔐 Easy login with email or phone\n"
                "* \u2601\ufe0f Chat history synced across devices\n* 🔍 Web search and deep thinking modes")
    content = page(NAV, "# Introducing the App", features, "Previous Release Next Release", "On this page",
                   "* Key features of the app:")
    assert extract_article(content, "Introducing the App | Example Docs") == features


def test_paragraph_repeated_inside_the_article_is_kept_once():
    content = page(f"# {TITLE}", P1, P2, P2, P3)
    assert extract_article(content, TITLE) == ARTICLE


def test_line_that_repeats_across_the_page_is_furniture():
    caption = "Model X runs on a laptop in this photo provided by Example Labs to members of the press this week."
    content = page(ARTICLE, NAV, caption, "Gallery", caption, "Gallery", caption)
    assert extract_article(content, None) == ARTICLE


def test_media_placeholders_are_dropped():
    content = page(P1, "[Video 3](https://www.youtube.com/watch?v=abc123)", P2, P3)
    assert extract_article(content, None) == ARTICLE


def test_list_of_statements_belongs_to_the_article():
    notes = ("* 🔹 Major boost in reasoning performance\n* 🔹 Stronger front-end development skills\n"
             "* 📜 Models are now released under the MIT License, just like before!\n"
             "* 🔗 Open-source weights are on the model hub")
    content = page(NAV, "# DeepSeek-V9 Release", notes, "Previous Release Next Release", "* Email\n* Discord")
    assert extract_article(content, "DeepSeek-V9 Release | DeepSeek API Docs") == notes


def test_page_with_no_prose_is_returned_whole():
    notes = "* 🔹 Faster\n* 🔹 Cheaper\n\n## API\n\n* 🔹 New endpoint"
    assert extract_article(notes, None) == notes


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (P1 + "\n\n## This post is for paid members only\n\nBecome a paid member for unlimited access.", True),
        (P1 + "\n\nTo access our exclusive content you’ll need to subscribe.\n\nAlready a subscriber?", True),
        (ARTICLE + "\n\n* Subscribers only\n\nLearn more", False),
    ],
)
def test_paywall_markers(content: str, expected: bool):
    assert is_paywalled(content) is expected
