// The few members of @novnc/novnc 1.7.0's RFB (core/rfb.js) the pane uses; the package ships no
// types. `urlOrChannel` is a raw channel here (lib/screen-vnc.ts), never a URL: the browser never
// dials the driver's door.
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, urlOrChannel: unknown, options?: { shared?: boolean });
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;
    viewOnly: boolean;
    focus(): void;
    disconnect(): void;
  }
}
