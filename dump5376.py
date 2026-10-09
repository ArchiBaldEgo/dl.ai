# -*- coding: utf-8 -*-
from ai.models import AIModelTestResult
from ai.arm_runner import _extract_code_from_response
from ai.services import code_carver as cc

for pk in (5376, 5377):
    r = AIModelTestResult.objects.get(pk=pk)
    raw = r.raw_response or ""
    print("=" * 26, "RES", pk, r.model_key, r.verdict, "raw_len", len(raw))
    lines = raw.splitlines()
    print("FIRST-8:")
    for i in range(min(8, len(lines))):
        print("%3d: %s" % (i, lines[i][:90]))
    # где jmp begin
    print("jmp-begin lines:", [i for i, l in enumerate(lines) if cc._JMP_BEGIN_RE.match(l)][:6])
    now = _extract_code_from_response(raw, r.file_extension_snapshot).strip()
    print("NOW_LEN", len(now), "head:", now[:120].replace(chr(10), " | "))

    # детально: что делает структурник
    import json
    anchors = [i for i, l in enumerate(raw.splitlines()) if cc._JMP_BEGIN_RE.match(l)]
    spl = raw.splitlines()
    for start in anchors:
        span = cc._asm_candidates_for_anchor(spl, start)
        print("anchor@%d ->" % start, span)
    r = None