// The chat pane was opened by another web site (`calu.web.pane.CONFIRM_PAGE`): show what that
// site asks for, and connect only when the person clicks. The real pane is loaded through a
// same-origin navigation, so the server then serves it (a fragment change alone would load
// nothing).
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const dir = params.get("dir");
  const session = params.get("session");
  const parts = [];
  if (dir) parts.push(`folder ${dir}`);
  if (session) parts.push(`session ${session}`);
  document.getElementById("target").textContent = parts.length
    ? `It asks for: ${parts.join(", ")}.`
    : "It names no folder and no session.";
  const go = document.getElementById("go");
  go.href = `/pane?confirmed=1${location.hash}`;
})();
