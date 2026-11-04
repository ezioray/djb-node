import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from djb_node.desktop.app import make_controller
from djb_node.desktop.nodecloud import NodeCloudClient

key = open(os.path.expanduser("~/.djb/secrets/testnode-client-key")).read().strip()
c = make_controller(home="/tmp/djb-wp6-verify",
                    cloud=NodeCloudClient("http://127.0.0.1:18090", key))
target = "/tmp/djb-wp6-export/earnings-v2.csv"
os.makedirs(os.path.dirname(target), exist_ok=True)
c.save_path = lambda suggested: target          # 用固定路径替代保存对话框（取证用）
res = c.action("earnings.export")
print("export:", res.ok, res.message)
data = open(target, "rb").read()
print("bytes:", len(data), "BOM:", data[:3] == b"\xef\xbb\xbf", "CRLF:", b"\r\n" in data)
print(data.decode("utf-8-sig").replace("\r\n", " | ")[:400])
