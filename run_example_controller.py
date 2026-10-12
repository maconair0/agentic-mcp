"""
Run the example controller, so there is a controller to onboard.

It is a controller nothing here was written for (tests/example_controller.py).
Start it, then onboard it while the agentic MCP runs:

    python run_example_controller.py              # it publishes an API document
    python run_example_controller.py --silent     # it publishes nothing: use its user guide

    python -m agentic_mcp onboard metro --url http://127.0.0.1:9100/api
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests import example_controller  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--silent"]
    silent = "--silent" in sys.argv[1:]
    port = int(args[0]) if args else int(os.getenv("EXAMPLE_CONTROLLER_PORT", "9100"))
    box = example_controller.Controller(port=port, describes_itself=not silent).start()
    print(f"example controller at {box.url}  " + ("(it publishes no API document)" if silent else
          f"(its API document: {box.url}/openapi.json)"), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        box.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
