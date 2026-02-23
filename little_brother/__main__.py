import signal
import sys

from .main import LittleBrother


def handle_exit(signum, frame):
    print(f"\n[LB] Received signal {signum}")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    lb = LittleBrother()
    lb.run()
