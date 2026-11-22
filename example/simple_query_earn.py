"""只读查询余币宝活期 USDT：python3 -m example.simple_query_earn。"""

import json
import os

from gate_api import ApiClient, EarnUniApi

try:
    from .earn_futures_transfer_common import TIMEOUT, load_env_file, make_configuration
except ImportError:
    from earn_futures_transfer_common import TIMEOUT, load_env_file, make_configuration


def main():
    load_env_file()
    config = make_configuration(
        os.getenv("GATE_HOST", "https://api.gateio.ws/api/v4").strip(),
        os.environ["GATE_API_KEY"],
        os.environ["GATE_API_SECRET"],
    )
    with ApiClient(config) as client:
        rows = EarnUniApi(client).list_user_uni_lends(currency="USDT", _request_timeout=TIMEOUT)
    print(json.dumps([row.to_dict() for row in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
