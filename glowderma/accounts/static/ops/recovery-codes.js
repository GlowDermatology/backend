/* Glow Derma Ops: the copy button of the recovery codes page (accounts/console/recovery_codes.html).
   A static file because the edge's CSP runs no inline script. Without JavaScript or the Clipboard API the
   button stays hidden; the .txt download link works on its own. The texts come from the template, in the
   console's language: data-copy-header (the first line of the copied text) and data-copied-label. */
(function () {
  "use strict";

  function codesText(list, header) {
    var codes = Array.prototype.map.call(list.querySelectorAll("code"), function (node) {
      return node.textContent.trim();
    });
    return header + "\n" + codes.join("\n") + "\n";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-copy-target]");
    if (!button || !navigator.clipboard || !window.isSecureContext) {
      return;
    }
    var list = document.getElementById(button.getAttribute("data-copy-target"));
    if (!list) {
      return;
    }
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(codesText(list, button.getAttribute("data-copy-header"))).then(function () {
        button.textContent = button.getAttribute("data-copied-label");
      });
    });
  });
})();
