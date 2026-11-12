/* Glow Derma Ops dashboards: number formats and theme-aware series colours for Unfold's Chart.js charts.
   Unfold builds the charts on window "load" (resolving var(--…) colours once); this runs after it.
   The words it prints (the currency, the scale units, the tooltip total) come translated from the page:
   the data-gd-* attributes of .gd-dash (reporting/dashboards/base.html). */
(function () {
  "use strict";

  var NBSP = " ";

  function grouped(value) {
    return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  }

  function words() {
    var root = document.querySelector(".gd-dash");
    return root ? root.dataset : {};
  }

  function compact(value, text) {
    var abs = Math.abs(value);
    var units = [[1e9, text.gdMlrd], [1e6, text.gdMln], [1e3, text.gdMing]];
    for (var i = 0; i < units.length; i++) {
      if (abs >= units[i][0]) {
        return (value / units[i][0]).toFixed(1).replace(/\.0$/, "").replace(".", ",") + " " + (units[i][1] || "");
      }
    }
    return grouped(value);
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function charts() {
    if (!window.Chart || !window.Chart.instances) return [];
    return Object.values(window.Chart.instances).filter(function (chart) {
      return chart.canvas && chart.canvas.closest("[data-gd-chart]");
    });
  }

  function recolour(chart) {
    chart.data.datasets.forEach(function (dataset) {
      if (dataset.gdColor) dataset.backgroundColor = cssVar(dataset.gdColor) || dataset.backgroundColor;
      dataset.borderColor = cssVar("--gd-surface") || dataset.borderColor;
    });
  }

  function setup() {
    var text = words();
    var som = " " + (text.gdSom || "");
    charts().forEach(function (chart) {
      var unit = chart.canvas.closest("[data-gd-chart]").getAttribute("data-gd-chart");
      var money = unit === "som";
      // Write to the raw config (chart.options is a resolver proxy: assigning into it recurses).
      var options = chart.config.options;
      options.plugins = options.plugins || {};
      options.plugins.tooltip = options.plugins.tooltip || {};
      options.plugins.tooltip.callbacks = {
        label: function (context) {
          var value = context.parsed.y || 0;
          return " " + context.dataset.label + ": " + grouped(value) + (money ? som : "");
        },
        footer: function (items) {
          var total = items.reduce(function (sum, item) { return sum + (item.parsed.y || 0); }, 0);
          return (text.gdTotal || "") + ": " + grouped(total) + (money ? som : "");
        },
      };
      options.scales.y.ticks.callback = function (value) {
        return money ? compact(value, text) : grouped(value);
      };
      recolour(chart);
      chart.update();
    });
  }

  window.addEventListener("load", function () {
    setup();
    new MutationObserver(function () {
      charts().forEach(function (chart) {
        recolour(chart);
        chart.update();
      });
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  });
})();
