# Patches applied to vendor.min.js after esbuild

Re-apply these by hand (or fix upstream) whenever the bundle is rebuilt.

## 1. OrbitControls `_onPointerUp` guard (2026-08-30)

three 0.185.1 OrbitControls has a pointer-bookkeeping bug: 3d-force-graph
dispatches a synthetic document-level `pointerup` (`pointerType: "touch"`, no
tracked `pointerId`) when a node drag ends, to resync the controls after
re-enabling them. `_removePointer` mis-splices `_pointers` against the unknown
id, and the touch-fallback branch in `_onPointerUp` then dereferences
`this._pointerPositions[e]` for a pointer it never saw a `pointerdown` for:
`TypeError: Cannot read properties of undefined (reading 'x')`.

Patch (one occurrence in the minified bundle):

```
- case 1:let e=this._pointers[0],t=this._pointerPositions[e];this._onTouchStart({pointerId:e,pageX:t.x,pageY:t.y});break
+ case 1:let e=this._pointers[0],t=this._pointerPositions[e];if(t)this._onTouchStart({pointerId:e,pageX:t.x,pageY:t.y});break
```

No behavior change on the happy path; the guarded branch only fired in the
already-corrupt state that threw.
