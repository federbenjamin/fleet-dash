# M12 N0 — Web Push compatibility evidence

Status: complete; automated and installed iPhone/macOS gates passed

Date: 2026-07-16/17

## Runtime compatibility

- Fleet launchd runtime: `/usr/bin/python3` 3.9.6.
- Installed Node runtime: 24.1.0; npm 11.3.0.
- Production dependency pinned by lockfile: `web-push` 3.6.7.
- `tests/web_push_probe.mjs --self-test` generates a P-256 recipient, encrypts the minimal payload
  with `aes128gcm`, adds VAPID authorization, and validates topic/payload disclosure without making a
  network request.
- The probe server keeps its VAPID private key and subscription in memory, binds to localhost by
  default, logs neither secret, and sends only after the user presses **Send test**.

## Live pre-change latency baseline

Measured against commit `78315e0` while the real 3.2 GB transcript index was 99.6% complete.

| Surface | p50 | p95 | Contract |
| --- | ---: | ---: | --- |
| `/api/fleet` client | 5.174 ms | 8.240 ms | pass, <250 ms |
| Main context client | 2.159 ms | 5.364 ms | pass, <250 ms |
| Agent context client | 2.073 ms | 48.935 ms | pass, <250 ms |
| History client | 4.576 ms | 8.540 ms | pass, <250 ms |
| Search client | 1.449 ms | 5.445 ms | pass, <250 ms |
| Action ping client | 0.667 ms | 1.273 ms | pass, <250 ms |
| Engine scan | 205.870 ms | 571.298 ms | comparison baseline |
| Desktop first useful | 198.752 ms | 233.503 ms | pass, <350 ms |
| Mobile 390×844 first useful | 192.341 ms | 241.383 ms | pass, <350 ms |
| Desktop render / poll | — | 4.2 / 66.1 ms | pass |
| Mobile render / poll | — | 4.7 / 69.1 ms | pass |

Commands:

```bash
python3 tests/perf_baseline.py --samples 40 --context-samples 10 --history-samples 20 \
  --search-samples 40 --search-query fleet --local-action-auth --action-samples 20 \
  --skip-corpus --assert-contract
node tests/browser_baseline.js http://127.0.0.1:8377/ 16 --assert-contract
node tests/web_push_probe.mjs --self-test
```

## Real-device release evidence

- Installed iPhone and macOS Fleet apps registered through the real private HTTPS origin only after
  the explicit **Enable notifications** action; both remained healthy in Fleet's redacted device
  projection.
- With both apps closed, fresh encrypted production notifications arrived with generic title/body
  and a nonzero app-icon badge.
- Tapping each native notification launched the installed Fleet app into the exact current
  Notification Center event.
- Production question, approval, and reply deliveries passed on both devices. The user confirmed the
  native interaction and final state after the N5 Snooze/Mute action flow.
- The same action failure has a deterministic exact-event fallback on clients that omit or cannot
  complete system action buttons.

The disposable compatibility probe remains available for future runtime upgrades:

```bash
node tests/web_push_probe.mjs --subject mailto:fleet-dash@localhost.invalid
```

Expose that localhost port through the same private HTTPS/tailnet path used for Fleet. The probe
stores no subscription or key after exit.
