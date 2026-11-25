"""Local CLI entrypoint for TechNews agent."""

from dataclasses import asdict
from datetime import datetime

from common.script_env import load_env


def main():
    load_env()

    from agent import run_turn
    from common.db import close_db_pool

    history: list[dict] = []

    try:
        print("Ready. Type 'exit' to quit.")

        while True:
            try:
                user_input = input("You: ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ["exit", "quit"]:
                    break

                # The machine's local zone, as a fixed offset: Windows gives no zone database name.
                answer = run_turn(user_input, history, channel="cli", zone=datetime.now().astimezone().tzinfo)
                print(f"Agent: {answer.text}")
                for source in answer.sources:
                    print(f"  [{source.n}] {source.title_zh or source.title} {source.url}")
                print()

                if answer.status != "error":
                    history.append({"role": "user", "parts": [{"text": user_input}]})
                    history.append(
                        {
                            "role": "model",
                            "parts": [{"text": answer.text}],
                            "status": answer.status,
                            "sources": [asdict(s) for s in answer.sources],
                        }
                    )

            except EOFError:
                break
            except KeyboardInterrupt:
                print("\n[Info] Interrupted. Continue typing or use 'exit' to quit.")
            except Exception as e:
                print(f"[Error] {e}\n")

    finally:
        close_db_pool()
        print("Exited. Database connections closed.")


if __name__ == "__main__":
    main()
