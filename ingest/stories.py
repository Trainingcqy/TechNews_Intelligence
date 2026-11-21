"""link stage: assign each embedded article to a story."""

from __future__ import annotations

from datetime import timedelta

from ingest import store

STORY_THRESHOLD = 0.85
STORY_WINDOW_DAYS = 3
STORY_MAX_SPAN_DAYS = 7


def joins_story(nearest: store.NearestStory | None) -> bool:
    return (
        nearest is not None
        and nearest.similarity >= STORY_THRESHOLD
        and nearest.span_after <= timedelta(days=STORY_MAX_SPAN_DAYS)
    )


def assign_story(cur, article_id: int) -> int:
    story_id = store.story_of(cur, article_id)
    if story_id is None:
        nearest = store.nearest_story(cur, article_id, window_days=STORY_WINDOW_DAYS)
        story_id = nearest.story_id if joins_story(nearest) else store.next_story_id(cur)
        # The nearest neighbour is kept below the threshold too, for replaying other thresholds offline.
        store.record_story_member(
            cur,
            article_id,
            story_id,
            nearest.article_id if nearest else None,
            nearest.similarity if nearest else None,
        )
    store.mark_ready(cur, article_id)
    return story_id


def link_pending(*, transaction: store.Transaction) -> int:
    """Processes every embedded article. An error stops the stage: stories are assigned in event order, and
    skipping an article would break that order. The next round starts again from the same article."""
    with transaction() as cur:
        article_ids = store.take_link(cur)
    for article_id in article_ids:
        with transaction() as cur:
            assign_story(cur, article_id)
    return len(article_ids)
