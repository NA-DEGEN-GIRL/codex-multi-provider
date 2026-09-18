"""Stage account-independent desktop assets before publishing a manager build."""
import json
import sys
from desktop_launch import find_app
from manager_core.desktop_bundle import prepare

if __name__ == '__main__':
    print(json.dumps(prepare(sys.argv[1], find_app()), ensure_ascii=False))
