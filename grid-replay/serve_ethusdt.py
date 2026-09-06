"""Open the existing replay UI with the downloaded ETHUSDT dataset."""
import os

from example_ethusdt_long import CSV


if __name__ == "__main__":
    os.environ["GRID_REPLAY_CSV"] = str(CSV)
    os.environ["GRID_REPLAY_SYMBOL"] = "ETHUSDT"
    from server import main

    main()
