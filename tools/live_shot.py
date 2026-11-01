"""真数据截图：从密钥文件读 key 到内存（不落盘、不打印），逐视图截图 + 导出视图文本。

用法::

    xvfb-run -a -s "-screen 0 1400x900x24" python tools/live_shot.py <outdir> [--base URL]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from djb_node.desktop.app import DesktopApp, _shoot, make_controller
from djb_node.desktop.nodecloud import NodeCloudClient

parser = argparse.ArgumentParser()
parser.add_argument("out", help="截图/文本输出目录")
parser.add_argument("--base", default="http://127.0.0.1:18090")
parser.add_argument("--key-file", default="~/.djb/secrets/testnode-client-key")
parser.add_argument("--home", default=os.environ.get("DJB_HOME", "/tmp/djb-wp6-live"),
                    help="临时 home（不碰 ~/.djb）；默认跟 DJB_HOME，避免截图与文本跑在不同 home 上")
parser.add_argument("--theme", default=None, choices=("day", "night"),
                    help="截图用哪套主题（默认读桌面端偏好，没有则日间）")
args = parser.parse_args()

out = args.out
os.makedirs(out, exist_ok=True)
key = open(os.path.expanduser(args.key_file)).read().strip()
cloud = NodeCloudClient(args.base, key, timeout=10.0)
controller = make_controller(home=args.home, cloud=cloud)
app = DesktopApp(controller, on_quit=lambda: None, prefs_home=args.home)
if getattr(args, "theme", None):
    app._install_theme(args.theme, animate=False, persist=False)   # noqa: SLF001 - 取证脚本
app.show_view('overview')
app._cancel_fade()                                     # noqa: SLF001 - 截图不等渐变
app._paint_backgrounds()                               # noqa: SLF001
print(_shoot(app, out)[:2])
for view in ('tasks', 'receipts', 'earnings', 'account'):
    text = app.show_view(view).plain_text()
    with open(os.path.join(out, 'live-' + view + '.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text)
app.stop()
print('done')
