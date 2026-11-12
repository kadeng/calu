// The page a browser gets for the address `calu web` printed (`?token=...`). The server has set
// the session cookie; this stores the second secret, the key, where only this origin can read
// it (localStorage belongs to scheme, host and port), and goes on to the address without the
// token. Every request that changes something carries the key (calu.js, triage.js, live.js).
(() => {
  const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
  try {
    localStorage.setItem("calu-key", meta("calu-key"));
  } catch (error) {
    document.getElementById("status").textContent =
      "calu web cannot store the key of this login: the browser blocks local storage for " +
      "this address. Allow it and open the printed address again.";
    return;
  }
  // Only ever onward within this interface, whatever the address said.
  const onward = new URL(meta("calu-onward"), location.origin);
  location.replace(onward.origin === location.origin ? onward.href : "/");
})();
