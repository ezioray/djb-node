import os, sys
sys.path.insert(0, '/vol2/@appdata/deepseek.harness/home/DJB联脑/repos/djb-node')
from djb_node.desktop.app import make_controller
from djb_node.desktop.nodecloud import NodeCloudClient
from djb_node.desktop.views import VIEW_KEYS
key = open(os.path.expanduser('~/.djb/secrets/testnode-client-key')).read().strip()
c = make_controller(home='/tmp/djb-wp6-live', cloud=NodeCloudClient('http://127.0.0.1:18090', key))
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
for view in VIEW_KEYS:
    open(os.path.join(out, view + '.txt'), 'w', encoding='utf-8').write(c.show(view).plain_text())
print('wrote', len(VIEW_KEYS))
