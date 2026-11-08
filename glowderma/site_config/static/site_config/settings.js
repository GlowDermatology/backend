/*
 * The ops console's settings page (glowderma/site_config/templates/site_config/console/settings.html).
 *
 * Alpine components registered on `alpine:init` (this file is loaded before Unfold's deferred Alpine starts;
 * the console's CSP allows no inline script):
 *   gdCard(bound)  — one card's form: «Saqlash» is enabled once a field differs from the value it was rendered
 *                    with (or the server sent the form back with errors), and shows «Saqlanmoqda…» on submit.
 *   gdLegal(opts)  — the legal texts: document and language tabs, the character counters and the per-language
 *                    "has text" dots, «E’lon qilish» (disabled while there are unsaved changes) and its dialog.
 * Plus: phone inputs ([data-gd-phone]) are masked as "+998 71 200 00 00"; leaving the page with unsaved
 * changes asks first; after a refused save the first invalid field gets the focus.
 * The server checks everything again: none of this is a security boundary.
 */
(function () {
  "use strict";

  // A character that counts as text: letters, digits, punctuation, symbols (site_config.selectors.has_visible_text).
  var VISIBLE = /[\p{L}\p{N}\p{P}\p{S}]/u;

  function changed(form) {
    var fields = form.querySelectorAll("input:not([type=hidden]), textarea, select");
    for (var i = 0; i < fields.length; i += 1) {
      if (fields[i].value !== fields[i].defaultValue) return true;
    }
    return false;
  }

  var OTHERS_DIRTY =
    "Boshqa bo‘limda saqlanmagan o‘zgarishlar bor: sahifa yangilanganda ular yo‘qoladi. Davom etasizmi?";

  /* Each card saves on its own, by a full page POST: unsaved edits of another card would be lost. */
  function confirmOthers(form, event) {
    var others = Array.prototype.filter.call(
      document.querySelectorAll("[data-gd-form][data-gd-dirty='1']"),
      function (other) {
        return other !== form;
      },
    );
    if (others.length && !window.confirm(OTHERS_DIRTY)) {
      event.preventDefault();
      return false;
    }
    return true;
  }

  /* Alpine shows an x-show element on the next animation frame: focus once it is displayed. */
  function focusCancel(root) {
    var tries = 0;
    (function attempt() {
      var cancel = root.querySelector("[data-gd-cancel]");
      if (cancel && cancel.offsetParent !== null) {
        cancel.focus();
      } else if ((tries += 1) < 20) {
        window.setTimeout(attempt, 25);
      }
    })();
  }

  function codePoints(text) {
    return Array.from(text.trim()).length;
  }

  /* "+998 71 200 00 00" while typing (the Next admin's maskPhone); the server normalises again. */
  function maskPhone(raw) {
    var digits = raw.replace(/\D/g, "");
    if (digits.indexOf("998") === 0 && (raw.trim().charAt(0) === "+" || digits.length > 9)) {
      digits = digits.slice(3);
    }
    digits = digits.slice(0, 9);
    if (!digits) return "";
    var groups = [digits.slice(0, 2), digits.slice(2, 5), digits.slice(5, 7), digits.slice(7, 9)];
    return "+998 " + groups.filter(Boolean).join(" ");
  }

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.hasAttribute("data-gd-phone")) return;
    var masked = maskPhone(target.value);
    if (masked !== target.value) target.value = masked;
  });

  var leaving = false;
  window.addEventListener("beforeunload", function (event) {
    if (leaving || !document.querySelector("[data-gd-form][data-gd-dirty='1']")) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;

    Alpine.data("gdCard", function (bound) {
      return {
        dirty: Boolean(bound),
        busy: false,
        bound: Boolean(bound),
        init: function () {
          this.mark();
        },
        refresh: function () {
          this.dirty = this.bound || changed(this.$refs.form);
          this.mark();
        },
        mark: function () {
          this.$refs.form.setAttribute("data-gd-dirty", this.dirty ? "1" : "0");
        },
        submitting: function (event) {
          if (!confirmOthers(this.$refs.form, event)) return;
          leaving = true;
          this.busy = true;
        },
      };
    });

    Alpine.data("gdLegal", function (options) {
      return {
        document: options.document || "terms",
        language: options.language || "uz",
        bound: Boolean(options.bound),
        dirty: Boolean(options.bound),
        busy: false,
        publishOpen: Boolean(options.publishOpen),
        publishing: false,
        values: {},
        init: function () {
          var self = this;
          this.$refs.form.querySelectorAll("textarea").forEach(function (area) {
            self.values[area.name] = area.value;
          });
          this.mark();
          this.$watch("publishing", function (value) {
            if (value) leaving = true;
          });
          if (this.publishOpen) focusCancel(this.$root);
        },
        open: function (documentKey, language) {
          this.document = documentKey;
          this.language = language;
          var self = this;
          this.$nextTick(function () {
            var area = self.$refs.form.querySelector("textarea[name='" + documentKey + "_" + language + "']");
            if (area) area.focus();
          });
        },
        refresh: function () {
          var self = this;
          this.$refs.form.querySelectorAll("textarea").forEach(function (area) {
            self.values[area.name] = area.value;
          });
          this.dirty = this.bound || changed(this.$refs.form);
          this.mark();
        },
        mark: function () {
          this.$refs.form.setAttribute("data-gd-dirty", this.dirty ? "1" : "0");
        },
        openPublish: function () {
          this.publishOpen = true;
          // Focus starts on «Bekor qilish» (the Next.js AlertDialog did the same): publishing is irreversible.
          focusCancel(this.$root);
        },
        closePublish: function () {
          if (this.publishing || !this.publishOpen) return;
          this.publishOpen = false;
          var button = this.$refs.publishButton;
          this.$nextTick(function () {
            if (button) button.focus();
          });
        },
        count: function (name) {
          return codePoints(this.values[name] || "");
        },
        complete: function (name) {
          return VISIBLE.test(this.values[name] || "");
        },
        submitting: function (event) {
          if (!confirmOthers(this.$refs.form, event)) return;
          leaving = true;
          this.busy = true;
        },
      };
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.querySelector("[data-gd-focus]");
    var id = root ? root.getAttribute("data-gd-focus") : "";
    if (!id) return;
    // After Alpine has shown the right legal tab (the server opens the first one with an error).
    window.setTimeout(function () {
      var field = document.getElementById(id);
      if (field) {
        field.focus();
        if (field.scrollIntoView) field.scrollIntoView({ block: "center" });
      }
    }, 0);
  });
})();
