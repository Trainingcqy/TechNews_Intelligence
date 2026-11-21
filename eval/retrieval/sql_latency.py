"""SQL latency of hybrid retrieval per time window (plan 29 step 10), the baseline of the vector index item in
section 30.

For the first N queries of queries.jsonl, builds the SQL and parameters that `fetch_hybrid_rows` sends for a
search_news call with default arguments, runs them under EXPLAIN ANALYZE and prints the median of the planning plus
execution time reported by the database, so the network between this machine and the database does not count.
Read-only; the embedding of each query is requested from Jina once.

    python eval/retrieval/sql_latency.py [--days 14 365] [--runs 20]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.retrieval.embeddings import get_query_embedding  # noqa: E402
from agent.retrieval.hybrid import fetch_hybrid_rows  # noqa: E402
from agent.retrieval.recall_profile import resolve_recall_profile  # noqa: E402
from agent.retrieval.search import MAX_ARTICLE_LIMIT, time_window  # noqa: E402
from agent.tools.registry import SearchNewsArgs  # noqa: E402
from eval.common import add_db_args, configure_stdout, readonly_cursor  # noqa: E402

QUERIES = Path(__file__).with_name("queries.jsonl")


class _CaptureCursor:
    """Takes the place of the cursor so that fetch_hybrid_rows hands over its SQL and parameters unexecuted."""

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return []


def default_candidate_limit() -> int:
    """The limit fetch_hybrid_rows receives for a search_news call with default arguments and reranking on:
    search_stories asks for three articles per story, lookup_candidates_by_query widens that for the reranker."""
    articles = min(SearchNewsArgs.model_fields["limit"].default * 3, MAX_ARTICLE_LIMIT)
    profile = resolve_recall_profile()
    return min(articles * profile.query_cand_multiplier, profile.query_cand_max)


def hybrid_sql(query: str, query_vec: list[float], *, days: int, limit: int, now: datetime) -> tuple[str, dict]:
    start_at, end_at = time_window(days, None, None, now=now)
    capture = _CaptureCursor()
    fetch_hybrid_rows(capture, query=query, limit=limit, query_vec=query_vec, start_at=start_at, end_at=end_at)
    return capture.sql, capture.params


def explain_ms(cur, sql: str, params: dict) -> float:
    cur.execute("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql, params)
    plan = cur.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    return plan[0]["Planning Time"] + plan[0]["Execution Time"]


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description="SQL latency of hybrid retrieval per time window.")
    parser.add_argument("--days", type=int, nargs="+", default=[14, 365])
    parser.add_argument("--runs", type=int, default=20, help="number of queries, from the top of queries.jsonl")
    add_db_args(parser)
    args = parser.parse_args(argv)

    lines = [line for line in QUERIES.read_text(encoding="utf-8").splitlines() if line.strip()]
    queries = [json.loads(line)["query"] for line in lines][: args.runs]
    with readonly_cursor(args.env_file) as cur:
        vectors = []
        for query in queries:
            vector = get_query_embedding(query)
            if vector is None:
                print(f"embedding failed, nothing measured: {query}")
                return 1
            vectors.append(vector)
        limit = default_candidate_limit()
        now = datetime.now(timezone.utc)
        print(f"queries={len(queries)} limit={limit}")
        for days in args.days:
            times = [
                explain_ms(cur, *hybrid_sql(query, vector, days=days, limit=limit, now=now))
                for query, vector in zip(queries, vectors)
            ]
            print(f"days={days}\tmedian={statistics.median(times):.1f} ms\tmax={max(times):.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
