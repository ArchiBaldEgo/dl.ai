import django; django.setup()
import json, time, os

# Test load-tree via the view
from django.test import RequestFactory
from ai.admin.arm import (
    admin_arm_solve_load_tree_view,
    admin_arm_solve_start_view,
    admin_arm_solve_status_view,
)
from django.contrib.auth import get_user_model
from ai.arm_runner import get_arm_run_snapshot

User = get_user_model()
u = User.objects.filter(is_staff=True, username="user_100105").first()
if not u:
    u = User.objects.filter(is_staff=True).first()
print(f"Using user: {u.username} (id={u.id})")

sid = "{4B242418-DFE8-48FB-A8C5-A3E6BDDA9EC0}"
rf = RequestFactory()

# Step 1: load-tree
print("\n=== STEP 1: load-tree ===")
req = rf.post("/ai/admin/arm/solve/load-tree/",
    data="course_id=1450",
    content_type="application/x-www-form-urlencoded; charset=UTF-8")
req.user = u
req.session = {"external_session_id": sid}
req.COOKIES = {}
resp = admin_arm_solve_load_tree_view(req)
data = json.loads(resp.content)
print(f"ok={data.get('ok')} task_count={data.get('task_count')}")
if not data.get("ok"):
    print(f"ERROR: {data.get('message')}")
    exit(1)

# Find a few .i86 and .cmp tasks from the tree
def collect_tasks(nodes, results):
    for n in nodes:
        if n.get("isFolder"):
            collect_tasks(n.get("children", []), results)
        else:
            results.append(n)

all_tasks = []
collect_tasks(data.get("tree", []), all_tasks)
print(f"Total leaf tasks: {len(all_tasks)}")

# Pick first 2 tasks for testing
test_tasks = all_tasks[:2]
node_ids = [t["nodeId"] for t in test_tasks]
print(f"Test node_ids: {node_ids}")
for t in test_tasks:
    print(f"  nodeId={t['nodeId']} name={t.get('name','')[:40]} has_statement={t.get('has_statement')}")

# Step 2: start batch solve
print("\n=== STEP 2: start batch solve ===")
form_data = f"course_id=1450&dl_test=1&file_extension=.i86&interface_language=Русский"
for nid in node_ids:
    form_data += f"&node_ids={nid}"
# Add one model
form_data += "&models=Web_DeepSeek"

req2 = rf.post("/ai/admin/arm/solve/start/",
    data=form_data,
    content_type="application/x-www-form-urlencoded; charset=UTF-8")
req2.user = u
req2.session = {"external_session_id": sid}
req2.COOKIES = {}
resp2 = admin_arm_solve_start_view(req2)
data2 = json.loads(resp2.content)
print(f"ok={data2.get('ok')}")
if not data2.get("ok"):
    print(f"ERROR: {data2.get('message')}")
    exit(1)

run_id = data2.get("run_id")
print(f"run_id={run_id}")

# Step 3: poll status
print("\n=== STEP 3: poll status ===")
for i in range(30):
    time.sleep(3)
    req3 = rf.get(f"/ai/admin/arm/solve/status/?run_id={run_id}")
    req3.user = u
    req3.session = {"external_session_id": sid}
    req3.COOKIES = {}
    resp3 = admin_arm_solve_status_view(req3)
    data3 = json.loads(resp3.content)
    run = data3.get("run", {})
    status = run.get("status")
    completed = run.get("completed_pairs", 0)
    total = run.get("total_pairs", 0)
    results = run.get("results", [])
    print(f"  poll {i+1}: status={status} completed={completed}/{total} results={len(results)}")
    if results:
        for r in results[-2:]:
            verdict = r.get("verdict")
            dl_comment = (r.get("dl_comment") or "")[:100]
            dl_error = (r.get("dl_error") or "")[:100]
            print(f"    result: verdict={verdict} model={r.get('model_title')} dl_comment={dl_comment} dl_error={dl_error}")
    if status in ("completed", "failed"):
        break

print("\n=== FINAL SNAPSHOT ===")
snap = get_arm_run_snapshot(run_id)
if snap:
    print(f"status={snap.get('status')}")
    report = snap.get("report") or {}
    print(f"report: solved={report.get('solved')} failed={report.get('failed')} skipped={report.get('skipped')}")
    for r in snap.get("results", []):
        print(f"  {r.get('task_name','')[:30]} | {r.get('model_title')} | verdict={r.get('verdict')} | dl_error={r.get('dl_error','')[:80]}")