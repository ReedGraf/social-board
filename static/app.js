// Optional enhancements; every feature also works with JS disabled.
(function () {
  "use strict";

  // Vote without reloading the page.
  document.addEventListener("submit", function (ev) {
    var f = ev.target;
    if (f.classList.contains("confirm") && !confirm(f.dataset.confirm || "Delete this? It can't be undone.")) {
      ev.preventDefault();
      return;
    }
    if (!f.classList.contains("vote") || !ev.submitter) return;
    ev.preventDefault();
    var data = new FormData(f);
    data.set("dir", ev.submitter.value);
    fetch(f.action, { method: "POST", body: data, headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.login) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
        f.classList.toggle("likes", j.vote === 1);
        f.classList.toggle("dislikes", j.vote === -1);
        f.querySelector(".up").setAttribute("aria-pressed", j.vote === 1);
        f.querySelector(".down").setAttribute("aria-pressed", j.vote === -1);
        f.querySelector(".score").textContent = j.score;
      })
      .catch(function () { f.submit(); });
  });

  // Picking a community color means "not the default" any more.
  // (Image resizing/thumbnails happen on the server, which converts everything to WebP.)
  document.addEventListener("change", function (ev) {
    var input = ev.target;
    if (input.type === "color" && input.form && input.form.elements.default_color) {
      input.form.elements.default_color.checked = false;
    }
  });

  // Ctrl+Enter (Cmd+Enter on Mac) sends the form you're typing in.
  document.addEventListener("keydown", function (ev) {
    var f = ev.target.form;
    if (ev.key !== "Enter" || !(ev.ctrlKey || ev.metaKey) || !f || f.method !== "post" || f.dataset.sent) return;
    ev.preventDefault();
    if (!f.checkValidity()) { f.reportValidity(); return; }
    f.dataset.sent = "1"; // ignore repeats while the page loads
    f.requestSubmit();
  });

  // Video ads play with sound. Browsers refuse unmuted autoplay until the visitor has interacted
  // with the site, so: try with sound; if refused, keep playing muted and unmute on the first
  // click, tap or key press. Visitors who asked their system to reduce motion get it paused.
  var ads = document.querySelectorAll(".adbox video");
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) {
    ads.forEach(function (v) { v.removeAttribute("autoplay"); v.pause(); });
  } else if (ads.length) {
    var unmuteLater = function () {
      var events = ["pointerdown", "keydown", "touchstart"];
      var go = function () {
        ads.forEach(function (v) { v.muted = false; if (v.paused) v.play().catch(function () {}); });
        events.forEach(function (t) { document.removeEventListener(t, go, true); });
      };
      events.forEach(function (t) { document.addEventListener(t, go, true); });
    };
    ads.forEach(function (v) {
      v.muted = false;
      var p = v.play();
      if (p && p.catch) p.catch(function () { v.muted = true; v.play().catch(function () {}); unmuteLater(); });
    });
  }

  // Live front page: new posts arrive over /live (server-sent events).
  var list = document.querySelector(".sitetable[data-live]");
  if (!list) return;
  var since = +list.dataset.since, seen = {}, queue = [], es = null, bar = null;
  function atTop() { return window.scrollY < 80; }
  function renumber() {
    list.querySelectorAll(".rank").forEach(function (r, i) { r.textContent = i + 1; });
  }
  function insert(html) {
    var box = document.createElement("div");
    box.innerHTML = html;
    var row = box.firstElementChild;
    if (!row || document.getElementById(row.id)) return;
    var empty = list.querySelector(".empty");
    if (empty) empty.remove();
    row.classList.add("fresh");
    // The page's color stylesheet doesn't know communities that weren't on it yet;
    // the server sends the shade that suits this viewer's theme.
    if (row.dataset.cc) {
      row.querySelectorAll("[class*='cc-']").forEach(function (el) { el.style.color = row.dataset.cc; });
    }
    list.insertBefore(row, list.firstChild);
    renumber();
  }
  function flush() {
    queue.splice(0).forEach(insert);
    if (bar) bar.hidden = true;
  }
  function showBar() {
    if (!bar) {
      bar = document.createElement("button");
      bar.className = "newposts";
      bar.addEventListener("click", function () { window.scrollTo(0, 0); flush(); });
      document.body.appendChild(bar);
    }
    bar.textContent = queue.length + (queue.length === 1 ? " new post" : " new posts") + " \u2191";
    bar.hidden = false;
  }
  function onPost(ev) {
    var parts = ev.data.split(" "), id = parts[0];
    since = Math.max(since, +parts[1]);
    if (seen[id] || document.getElementById("t_" + id)) return;
    seen[id] = true;
    fetch("/live/post/" + encodeURIComponent(id), { credentials: "same-origin" })
      .then(function (r) { return r.status === 200 ? r.text() : ""; })
      .then(function (html) {
        if (!html) return;
        if (atTop()) insert(html); else { queue.push(html); showBar(); }
      });
  }
  function connect() {
    if (es) return;
    es = new EventSource(list.dataset.live + "?since=" + since);
    es.addEventListener("post", onPost);
  }
  function disconnect() { if (es) { es.close(); es = null; } }
  window.addEventListener("scroll", function () { if (queue.length && atTop()) flush(); }, { passive: true });
  // Only hold a connection while the tab is visible; catch up via ?since= on return.
  document.addEventListener("visibilitychange", function () { if (document.hidden) disconnect(); else connect(); });
  if (!document.hidden) connect();
})();
