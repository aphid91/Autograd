// A brief message at the bottom of the screen that fades by itself. One at a
// time: a new toast replaces the one showing.

let el = null, timer = 0;

export function toast(text, ms = 3200) {
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    document.body.append(el);
  }
  el.textContent = text;
  el.dataset.show = "true";
  clearTimeout(timer);
  timer = setTimeout(() => { el.dataset.show = "false"; }, ms);
}
