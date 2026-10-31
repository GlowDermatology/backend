/* Glow Derma Ops dashboards: number formats and theme-aware series colours for Unfold's Chart.js charts.
   Unfold builds the charts on window "load" (resolving var(--…) colours once); this runs after it. */
(function () {
  "use strict";

  var NBSP = " ";

  function grouped(value) {
    return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  }

  function compact(value) {
    var abs = Math.abs(value);
    var units = [[1e9, " mlrd"], [1e6, " mln"], [1e3, " ming"]];
    for (var i = 0; i < units.length; i++) {
      if (abs >= units[i][0]) {
        return (value / units[i][0]).toFixed(1).replace(/\.0$/, "").replace(".", ",") + units[i][1];
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
          return " " + context.dataset.label + ": " + grouped(value) + (money ? " so\u2018m" : "");
        },
        footer: function (items) {
          var total = items.reduce(function (sum, item) { return sum + (item.parsed.y || 0); }, 0);
          return "Jami: " + grouped(total) + (money ? " so\u2018m" : "");
        },
      };
      options.scales.y.ticks.callback = function (value) {
        return money ? compact(value) : grouped(value);
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
