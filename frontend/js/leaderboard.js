/* Admin dashboard: full game leaderboard (minimum 10 submissions, no top-10 cap). */
(function ($) {
  var rows = [];
  var sortKey = "count";
  var sortDir = -1; // -1 highest first, 1 lowest first

  function showError(msg) {
    $("#lb-error").text(msg || "").prop("hidden", !msg);
  }

  function percent(accuracy) {
    return (accuracy * 100).toFixed(1) + "%";
  }

  function sorted() {
    var key = sortKey;
    var dir = sortDir;
    return rows.slice().sort(function (a, b) {
      if (a[key] !== b[key]) return (a[key] < b[key] ? -1 : 1) * dir;
      var name = a.username.toLowerCase().localeCompare(b.username.toLowerCase());
      return name || (a.username < b.username ? -1 : 1);
    });
  }

  function paintHeaders() {
    $("#lb-sort-count, #lb-sort-accuracy").each(function () {
      var key = this.id === "lb-sort-count" ? "count" : "accuracy";
      var label = key === "count" ? "Submissions" : "Accuracy";
      var active = sortKey === key;
      $(this).toggleClass("sort-active", active)
        .text(active ? label + (sortDir < 0 ? " ▼" : " ▲") : label);
    });
  }

  function render() {
    paintHeaders();
    var ordered = sorted();
    var $body = $("#lb-table tbody").empty();
    if (!ordered.length) {
      $("<tr>").append($("<td class='muted' colspan='4'>").text("No qualifying players.")).appendTo($body);
      return;
    }
    $.each(ordered, function (i, row) {
      var $tr = $("<tr>");
      $("<td>").text(i + 1).appendTo($tr);
      $("<td>").text(row.username).appendTo($tr);
      $("<td>").text(row.count).appendTo($tr);
      $("<td>").text(percent(row.accuracy)).appendTo($tr);
      $body.append($tr);
    });
  }

  function chooseSort(key) {
    if (sortKey === key) sortDir = -sortDir;
    else { sortKey = key; sortDir = -1; }
    render();
  }

  function load() {
    showError("");
    sortKey = "count";
    sortDir = -1;
    var mode = $("#lb-mode").val();
    Admin.api("GET", "/api/admin/leaderboard?game_type=" + mode).done(function (data) {
      rows = data.top_contributors;
      var n = rows.length;
      $("#lb-info").text(n + " player" + (n === 1 ? "" : "s") + " with at least " +
        data.minimum_contributions + " submissions");
      render();
    }).fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  $(function () {
    $("#lb-mode").on("change", load);
    $("#lb-refresh").on("click", load);
    $("#lb-sort-count").on("click", function () { chooseSort("count"); });
    $("#lb-sort-accuracy").on("click", function () { chooseSort("accuracy"); });
    $("#lb-sort-count, #lb-sort-accuracy").on("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        $(this).trigger("click");
      }
    });
  });
  $(document).on("admin:tab", function (_e, name) {
    if (name === "leaderboard") load();
  });
})(jQuery);
