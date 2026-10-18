# Zone Batch Scanning for single-surface scanners (Epson V600 / V-series flatbed+TPU)

Status: implemented in a local checkout (not upstream). Diffs are a single `git apply`-able patch; tests added under `tests/`.
Audience: local development against this checkout.

## 1. Problem

NegPy's **Batch** workflow is frame-based and only activates for roll/strip feeders
(`_reaches_a_strip(caps)` = `caps.adapter_frame_capacity is not None or caps.roll_discovery`,
in `negpy/desktop/view/sidebar/scan.py`). A flatbed+TPU device like the Epson Perfection
V600 (`epkowa:interpreter:*`) has neither, so it always runs in single-**Window** mode:
one crop region, one scan, no queue.

The V600's transparency unit is one physical surface (~68.6 × 237.0 mm per
`_detect_caps`'s probe comment in `sane_backend.py`) holding several negatives. Users want
to lay out N crop regions on one preview and scan each as its own image — "zone batch".
There is currently no multi-ROI scan and no scan-once-split-many.

This proposal scopes **multi-ROI zone batch** only. Split-once-scan-many (auto frame
detection inside a single flatbed transparency image) is a separate feature, deferred (see
§10).

## 2. Goals / non-goals

Goals:
- Let the operator lay out **N crop windows** on a low-DPI transparency preview and scan
  them in one batch, one output file per zone, frame-numbered like existing batches.
- Reuse the existing window → device-geometry plumbing (normalized window already maps to
  `-l/-t/-x/-y` mm on epkowa). **No change to the SANE backend's scan path.**
- Gate strictly: strip/roll feeders and single-scan flatbeds keep today's behavior.

Non-goals (v1):
- Driving the V600's vendor `--bay 1..6` option (Epson-specific, closed `epkowa`
  backend; bay indices would not survive a replug).
- Per-zone film-type / exposure / IR. The V600 exposes no IR channel via SANE anyway
  (no RGBI mode, no `--infrared`, no IR source — confirmed from `scanimage --help`).
  IR capture stays disabled on this device.
- Scan-once-split-many (§10 follow-up).
- Multi-zone on strip feeders (their batch UI already covers per-frame windows).

## 3. Current code map (all verified in this checkout)

| Concern | Location |
|---|---|
| Capability struct | `negpy/infrastructure/scanners/base.py` → `ScannerCapabilities` |
| Capability detection | `negpy/infrastructure/scanners/sane_backend.py` → `_caps_from_options` |
| Strip-gate helper | `negpy/desktop/view/sidebar/scan.py` → `_reaches_a_strip(caps)` |
| Scan settings | `negpy/infrastructure/scanners/settings.py` → `ScannerSettings`, `resolve_batch_selection` |
| Scan params (normalized window) | `negpy/infrastructure/scanners/params.py` → `ScanParams.window: tuple[x1,y1,x2,y2] | None` (0..1) |
| Window → device mm | `sane_backend.py` → `_apply_scan_window`, `_window_to_option_values` |
| Transparency source resolve | `sane_backend.py` → `_resolve_transparency_source` (SOURCE_MAP: `transparency`, `transparency unit`, `tpu`, `film`, …) |
| Film type resolve | `sane_backend.py` → `_resolve_film_type` |
| Single-window preview dialog | `negpy/desktop/view/widgets/quick_scan_preview_dialog.py` → `QuickScanPreviewDialog` |
| Strip preview dialog (multi-frame) | `negpy/desktop/view/widgets/strip_preview_dialog.py` → `StripPreviewDialog` |
| Batch worker | `negpy/desktop/workers/scan_worker.py` → `BatchRequest`, `run_batch` |
| Per-scan service call | `negpy/services/scanning/service.py` → `run_scan(device_id, params, progress, cancel)` |
| Result writer (seq naming) | `negpy/services/scanning/service.py` → `write_result(..., seq=frame)` |
| Preview entry point | `scan.py` → `_on_set_scan_window` (strips → `StripPreviewDialog`; else `QuickScanPreviewDialog`) |

Key facts that keep the backend untouched:
- `ScanParams.window` is already a normalized 0..1 rect; `_apply_scan_window` maps it to
  the device's tl/br geometry options in mm (epkowa `-l/-t/-x/-y`), clamped to
  `max_area_mm`.
- For flatbed+TPU devices `_detect_caps` already switches the device to the transparency
  source before reading options, so `caps.max_area_mm` reflects the **TPU** area
  (68.6×237.0 on the V600), not the platen. Normalized zone rects therefore land on the
  right physical region.
- The scan path already applies `source` + `film_type` per scan
  (`_resolve_transparency_source`, `_resolve_film_type`), so each zone scan is correctly
  a transparency negative/positive scan with no extra code.

## 4. Design

### 4.1 Capability: `zone_batch`

Add to `ScannerCapabilities` (base.py):

```python
#: Flatbed+TPU "single-surface" device: no frame slots, but the operator can lay out
#: several crop windows on one transparency preview and scan them as a batch.
zone_batch: bool = False
```

Set it in `_caps_from_options` (sane_backend.py). The condition — a transparency source
exists **and** the device is not a strip/roll feeder:

```python
zone_batch = (
    any(mode == ScanMode.TRANSPARENCY for mode in sources)
    and adapter_frame_capacity is None
    and not roll_discovery
)
```

(`ScanMode.TRANSPARENCY` is what `_SOURCE_MAP` yields for the V600's
`--source "Transparency Unit"`.) Dedicated film scanners that *also* have a transparency
source but expose frame slots stay on the existing batch path.

### 4.2 Settings: zones

In `ScannerSettings` (settings.py):

```python
#: Ordered list of normalized (x1,y1,x2,y2) zone rects for zone_batch devices.
#: Order is scan order; each zone becomes one output file, numbered 1..N.
zones: list[tuple[float, float, float, float]] | None = None
```

Persist exactly like `scan_window` today (override/settings round-trip). Add a resolver
mirroring `resolve_batch_selection`:

```python
def resolve_zone_selection(settings) -> tuple[tuple[int, ...], dict[int, tuple[float,float,float,float]]]:
    """(zone indices, per-zone windows) for a zone batch. Empty list -> no zones -> caller
    falls back to a single scan with `scan_window`."""
    zones = settings.zones or []
    return tuple(range(1, len(zones) + 1)), {i: z for i, z in enumerate(zones, start=1)}
```

### 4.3 UI: zone editor on the preview

The existing `QuickScanPreviewDialog` does preview-then-one-window. Zone mode (a flag on
the dialog) replaces the single rect canvas with a **multi-rect zone canvas**
(`widgets/zone_canvas_label.py`):

- Every zone is a **persistent, numbered box** drawn over the preview; zones never
  disappear once created, so the operator always sees what they are working with.
- **Draw-to-add**: dragging a fresh box in empty space creates a zone immediately
  (committed on mouse release) — no separate "add to list" step.
- The **active zone** (most recent, or picked via the ◀ ▶ arrows or a border click) is
  highlighted with corner handles: interior drag moves it, corner drag resizes it.
- Clicking a zone's **border** selects it without dragging, so Remove acts on the box
  the operator is pointing at.
- Toolbar, top bar of the dialog, left of Preview: **◀ ▶** (switch active zone),
  **＋ Add Zone** (duplicates the active zone, or a centered box when none exist),
  **− Remove Zone** (removes the active zone). Keyboard: `A`/`+` adds, `Delete`
  removes, `Ctrl+←`/`Ctrl+→` cycles.
- No zone list strip, no Load/Update buttons — zones live on the canvas only.
- Zone rects are stored **normalized** (0..1 of the preview) exactly as
  `dialog.window()` does today, so they map through `_apply_scan_window` unchanged.
- Reopening the dialog passes `settings.zones` in as `initial_zones`; the saved boxes
  render once the preview pixmap lands.
- Window validation: reject zones with near-zero area (< ~1 mm on the TPU) and zones
  outside the transparency area after clamping; keep existing clamp behavior
  (`scan_window_to_area` in params.py clamps to `max_area_mm`).

In `scan.py` `_on_set_scan_window`: when `device.capabilities.zone_batch` and not
`_reaches_a_strip(...)`, open the dialog in zone mode; on accept, write
`settings.zones` (and clear `scan_window`), refresh `_update_scan_window_status`.

Labels: `scan_window_row_label` → **"Zones"** when `caps.zone_batch` (extend the
existing `is_strip` branch at `scan.py:886`; note `is_strip` and `zone_batch` are mutually
exclusive by construction). `scan_window_btn` → **"Preview zones…"** with tooltip
"Preview the transparency area and lay out crop zones; each zone scans as its own image".

### 4.4 Worker: zone batch

Extend `BatchRequest` (scan_worker.py) with an optional zones payload — a list of rects is
just `frame_windows` keyed 1..N with `frames=(1..N)`, so the **simplest correct change is
to build the existing `BatchRequest` from zones** rather than add a new request type:

```
frames      = tuple(range(1, len(zones)+1))
frame_windows = {i+1: zone for i, zone in enumerate(zones)}
```

`run_batch` already does, per frame: check cancel → `window = frame_windows.get(frame,
req.params.window)` → `service.run_scan(...)` → `write_result(seq=frame)`. The one real
worker change: flatbeds have **no SANE frame-selection option**, so a zone batch must not
put the zone index into `params.frame` (the backend rejects `frame=1` with "no
frame-selection option"). `BatchRequest` gains `address_frames: bool = True`; zone batches
set it `False`, which scans with `frame=None`/`frame_offset_mm=0.0` (the zone index is used
only for output naming `seq`). Also:

- Rename the progress phase string for zones ("Zone 3 of 6 — Scanning") — the `position`
  string in `run_batch` currently hardcodes "Frame N of M"; parametrize it on the request
  (`unit_name="Frame"|"Zone"`).
- Keep the power assertion (`acquire_unattended_power_assertion`) — a zone batch is long.

File naming: reuse `write_result(seq=zone_index)` and the existing filename pattern, which
already appends a sequence (e.g. `strip-01.tif`). A zone batch of the same negative count
produces `…-01` … `…-06`.

### 4.5 Scan entry point

`scan.py` `_on_scan`: when `caps.zone_batch` and `settings.zones` is non-empty, build the
`BatchRequest` from `resolve_zone_selection(...)` and `controller.start_batch(...)` —
the same call the strip path uses today. When `zones` is empty/None, fall through to the
current single-window scan. `eject_when_done` stays False for flatbeds (no eject option on
`epkowa`).

## 5. Data flow

```
Scan tab (zone_batch) ─ Preview zones… ─ QuickScanPreviewDialog(zone mode)
      │  user draws N boxes on the canvas (auto-added, persisted on Apply/Scan)
      ▼
settings.zones = [r1..rN] (normalized)
      │  Scan
      ▼
resolve_zone_selection → frames=(1..N), frame_windows={i: ri}
      ▼
BatchRequest(frames=(1..N), frame_windows={ri}, params.dpi/depth/source…,
             unit_name="Zone", address_frames=False)
      ▼
run_batch: per zone → service.run_scan(window=ri, frame=None) → write_result(seq=i)
      ▼
Outputs: <pattern>-01.tif … -0N.tif, imported as N frames
```

The SANE backend is untouched: each `run_scan` applies source "Transparency Unit",
film-type, resolution, and `_apply_scan_window(window=ri)` exactly as today.

## 6. V600 specifics (verified from `scanimage --help` in this session)

- `--source Flatbed|Transparency Unit` → `_resolve_transparency_source` finds
  "Transparency Unit" (mapped in `_SOURCE_MAP`).
- `--film-type Positive Film|Negative Film` — inactive until the transparency source is
  selected; `_detect_caps` already switches source first, and the scan path sets it after
  the source switch, so this activates correctly.
- `--resolution` is capped at 3200 while `--x-resolution`/`--y-resolution` go to 6400 —
  NegPy already handles this epkowa quirk (V500 reference logic in `_resolve_square_resolutions`
  and capability tests). Zone batches must not pick a DPI that only one axis supports when
  the device caps `resolution`; reuse the existing DPI-stop logic untouched.
- No `ae`, no IR → those controls stay hidden; no action needed.

## 7. Edge cases

- **Zero/overlapping zones**: reject near-zero area (min ~1 mm on the TPU); overlap
  allowed (operator may intentionally overlap), no dedupe, but note the scan-time cost.
- **Zone partially outside the TPU**: clamp via `scan_window_to_area` (existing behavior).
- **Cancel mid-batch**: `run_batch` already breaks on `_cancel_event`, reports completed
  zones via `batch_finished`; no change.
- **Mid-batch error**: existing `outcome=("error", ...)` path; completed zones still import.
- **Replug / device ID change**: zone rects are normalized and device-independent; the
  `epkowa:interpreter:00X:00Y` suffix may change, `settings.zones` survives.
- **Legacy settings**: a saved `scan_window` with no `zones` keeps the single-window
  behavior — backward compatible.
- **Simulated backend**: add zone support to `SimulatedBackend` so worker tests run
  headless (it already mimics frames/windows).

## 8. Test plan

Mirror `tests/scanners/` conventions; add:

1. `test_zone_capability.py` — flatbed+TPU fixture (epkowa-style options:
   `source` = Flatbed|Transparency Unit, no frame capacity, no roll discovery) ⇒
   `zone_batch True`; strip-feeder fixture ⇒ `zone_batch False`; flatbed-without-TPU ⇒
   `zone_batch False`.
2. `test_zone_settings.py` — `resolve_zone_selection` ordering, empty→fallback,
   persistence round-trip of `zones`.
3. `test_zone_scan_worker.py` — SimulatedBackend: N zones ⇒ N scans with the right
   windows and `seq` names, **`params.frame` stays `None`** (address_frames=False);
   cancel after zone 2 ⇒ completed zones reported, no crash; mid-batch backend error ⇒
   `("error", msg)` and completed zones imported.
4. `test_zone_canvas_label.py` — real Qt (offscreen) mouse tests: draw-to-add, second
   draw adds a second zone, border click selects without moving, interior drag moves,
   corner drag resizes, clear; plus the zone-mode dialog (initial zones render, Add
   duplicates/Remove deletes, arrows cycle, Load/Update gone) and pure `hit_zone_edge` /
   `default_zone` math.
5. Regression: existing `test_sane_window.py`, `test_sane_refresh.py`,
   `test_service_backend_selection.py` must stay green (single-window path untouched).

## 9. Effort

| File | Change | Size |
|---|---|---|
| `base.py` | +1 capability field | ~2 LOC |
| `sane_backend.py` | +zone_batch in `_caps_from_options` | ~5 LOC |
| `settings.py` | `zones` field + `resolve_zone_selection` | ~15 LOC |
| `scan.py` | labels, `_on_set_scan_window` zone branch (initial_zones), `_on_scan` zone branch, status detail | ~110 LOC |
| `zone_canvas_label.py` | new multi-rect canvas (draw/add/select/move/resize/paint) | ~270 LOC |
| `quick_scan_preview_dialog.py` | zone mode: top-bar Add/Remove/arrows, shortcuts, initial zones | ~150 LOC |
| `scan_worker.py` | `unit_name` + `address_frames` | ~10 LOC |
| `tests/` | 4 new test modules (capability, settings, worker, canvas/dialog) | ~550 LOC |

Total ~1,300 LOC including tests, no SANE scan-path change (except `get_devices(True)`
local-only enumeration). The one sizable piece is the zone canvas; everything else is
wiring that reuses existing batch machinery.

## 10. Follow-ups (explicitly out of scope)

- **Scan-once-split-many**: detect N frames inside one flatbed transparency image and make
  each a frame. The single-frame detector (`get_autocrop_coords` /
  `detect_film_bounds_with_confidence`) would need a multi-frame clustering pass
  (candidate clustering already exists internally — `_cluster_film_candidates`). This is
  the higher-value feature for unattended V600 use; zone batch is the UI-first 80%.
- `--bay` driver scanning (Vendor-specific, closed backend; only if `epkowa` exposes
  stable bay indices across replug).
- Per-zone film-type/exposure presets.
