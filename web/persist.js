// Whether this page may write to the browser's storage. The editor may;
// presentation mode (present.js) may not -- a presentation link opened by
// someone else must not overwrite their own panel preferences, toolbar,
// layer panel size or saves. Every such write asks here first. (The sound
// button's own setting is the viewer's choice, and stays writable.)
let enabled = true;
export const persistEnabled = () => enabled;
export function freezePersist() { enabled = false; }
// The editor's preview (main.js enterPreview()) freezes the stores while
// the presentation's views are applied, and thaws them on the way back,
// after putting the editor's own view back -- so the preview leaves the
// player's preferences exactly as they were.
export function unfreezePersist() { enabled = true; }
