// Keep the screen on: Screen Wake Lock API in secure contexts (HTTPS or localhost),
// otherwise a tiny muted looping video started on the first user gesture.

// keepawake.mp4 is a 2.4 KB silent 16x16 black H.264 clip (generated with ffmpeg).
export class ScreenKeeper {
  constructor(onMode) {
    this.onMode = onMode;
    this.mode = "off"; // off | wakelock | video | needs_tap | unsupported
    this.lock = null;
    this.video = null;
    this.enabled = false;
    this.allowVideo = true; // the fallback when the browser's own screen lock is not available
    this._onVis = () => {
      if (document.visibilityState === "visible" && this.enabled) this._acquire();
    };
    this._onGesture = () => {
      if (this.enabled && this.allowVideo && this.mode !== "wakelock") this._startVideo();
    };
  }

  /** Allow or forbid the video fallback; takes effect at once. */
  setVideo(allowed) {
    this.allowVideo = allowed;
    if (!allowed && this.video) { this.video.pause(); this.video.remove(); this.video = null; }
    if (this.enabled) this._acquire();
  }

  _set(mode) {
    this.mode = mode;
    this.onMode && this.onMode(mode);
  }

  enable() {
    this.enabled = true;
    document.addEventListener("visibilitychange", this._onVis);
    document.addEventListener("pointerdown", this._onGesture, { passive: true });
    document.addEventListener("keydown", this._onGesture);
    this._acquire();
  }

  disable() {
    this.enabled = false;
    document.removeEventListener("visibilitychange", this._onVis);
    document.removeEventListener("pointerdown", this._onGesture);
    document.removeEventListener("keydown", this._onGesture);
    if (this.lock) this.lock.release().catch(() => {});
    this.lock = null;
    if (this.video) { this.video.pause(); this.video.remove(); this.video = null; }
    this._set("off");
  }

  async _acquire() {
    if (this.lock && !this.lock.released) { this._set("wakelock"); return; }
    if ("wakeLock" in navigator && window.isSecureContext) {
      try {
        this.lock = await navigator.wakeLock.request("screen");
        this.lock.addEventListener("release", () => {
          if (this.enabled && document.visibilityState === "visible") this._acquire();
        });
        this._set("wakelock");
        return;
      } catch (e) {
        // denied (e.g. battery saver) or needs a gesture: fall back
      }
    }
    if (!this.allowVideo) { this._set("no_wakelock"); return; }
    if (this.video && !this.video.paused) { this._set("video"); return; }
    this._set("needs_tap");
  }

  _startVideo() {
    try {
      if (!this.video) {
        const v = document.createElement("video");
        v.muted = true; v.loop = true; v.playsInline = true;
        v.setAttribute("playsinline", ""); v.setAttribute("muted", "");
        v.src = new URL("./keepawake.mp4", import.meta.url).href;
        v.style.cssText = "position:fixed;width:2px;height:2px;opacity:0.01;pointer-events:none;inset-block-end:0;inset-inline-start:0";
        document.body.appendChild(v);
        this.video = v;
      }
      this.video.play().then(() => this._set("video")).catch(() => this._set("needs_tap"));
    } catch (e) {
      this._set("unsupported");
    }
  }
}
