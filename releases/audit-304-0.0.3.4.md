# ir.0.0.3.4 — audit/transfer record

Source: 34146649a923ed28a638554fa6593495dcebac96 (main 0.0.2.77)
Current SHA: b7caad7e0b2bcc42ed79aed5d8590bedc90c155f
Status: BLOCKED / NOT VERIFIED

Scope preserved (R1–R6 from #107 / #278):
- R1 isolation / VM resources
- R2 GPU denial / control-stop / VRAM release
- R3 safe pause / fail / reacquire
- R4 independent audit / static checks / reviewer
- R5 24/7 schedule — requires R1–R4 + owner activation
- R6 evidence / exact version / SHA / digests / topology

No code changes; no deployment; no real VM/GPU access; no push/release.
Transfer to: #301 (ir.0.0.3.1 architecture delta), #302 (i.0.0.3.2 software defects), #303 (i.0.0.3.3 UX approval).
