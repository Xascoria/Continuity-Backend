/* Admin dashboard: Database tab (view / edit / add / delete rows of any table). */
(function ($) {
  var PAGE_SIZE = 25;
  var tables = {};      // name -> {name, primary_key, columns[]}
  var current = null;   // selected table meta
  var offset = 0;
  var total = 0;

  function showError(msg) {
    $("#db-error").text(msg || "").prop("hidden", !msg);
  }

  function errorText(xhr) {
    var d = xhr.responseJSON && xhr.responseJSON.detail;
    if (typeof d === "string") return d;
    if (d) return JSON.stringify(d);
    return "Request failed (" + xhr.status + ")";
  }

  function rowsUrl(suffix) {
    return "/api/admin/tables/" + encodeURIComponent(current.name) + "/rows" + (suffix || "");
  }

  // ---- table list ---------------------------------------------------------

  function loadTables() {
    Admin.api("GET", "/api/admin/tables").done(function (list) {
      var $sel = $("#table-select").empty();
      tables = {};
      $.each(list, function (_, t) {
        tables[t.name] = t;
        $("<option>").val(t.name).text(t.name).appendTo($sel);
      });
      if (list.length) selectTable($sel.val());
    }).fail(function (xhr) { showError(errorText(xhr)); });
  }

  function selectTable(name) {
    current = tables[name];
    offset = 0;
    renderHead();
    loadRows();
  }

  // ---- rendering ----------------------------------------------------------

  function renderHead() {
    var $tr = $("<tr>");
    $.each(current.columns, function (_, c) {
      $("<th>").text(c.name + (c.primary_key ? " (pk)" : ""))
        .attr("title", c.type + (c.nullable ? ", nullable" : ""))
        .appendTo($tr);
    });
    $("<th>").text("").appendTo($tr);
    $("#db-table thead").empty().append($tr);
  }

  function cellText($td, value) {
    if (value === null || value === undefined) {
      $td.append($("<em>").text("null"));
    } else {
      $td.text(String(value));
    }
  }

  function newRowTr() {
    var $tr = $("<tr class='new-row'>");
    $.each(current.columns, function (_, c) {
      var $in = $("<input type='text'>").attr("data-col", c.name)
        .attr("placeholder", c.has_default ? "default" : c.type);
      $("<td>").append($in).appendTo($tr);
    });
    var $add = $("<button>").text("Add row").on("click", function () { addRow($tr); });
    $("<td class='actions'>").append($add).appendTo($tr);
    return $tr;
  }

  function dataTr(row) {
    var pk = current.primary_key;
    var $tr = $("<tr>").data("row", row);
    $.each(current.columns, function (_, c) {
      var $td = $("<td>");
      if (c.primary_key) $td.addClass("pk");
      cellText($td, row[c.name]);
      $td.attr("data-col", c.name).appendTo($tr);
    });
    var $actions = $("<td class='actions'>").appendTo($tr);
    $("<button class='secondary'>").text("Edit").on("click", function () { editRow($tr); }).appendTo($actions);
    $("<button class='danger'>").text("Delete").on("click", function () {
      if (confirm("Delete " + current.name + " row " + pk + " = " + row[pk] + "?")) deleteRow(row[pk]);
    }).appendTo($actions);
    return $tr;
  }

  function render(rows) {
    var $body = $("#db-table tbody").empty().append(newRowTr());
    $.each(rows, function (_, r) { $body.append(dataTr(r)); });
    var from = total ? offset + 1 : 0;
    $("#db-info").text(from + "-" + (offset + rows.length) + " of " + total + " rows");
    $("#db-prev").prop("disabled", offset <= 0);
    $("#db-next").prop("disabled", offset + PAGE_SIZE >= total);
  }

  // ---- actions ------------------------------------------------------------

  function loadRows() {
    showError("");
    Admin.api("GET", rowsUrl("?limit=" + PAGE_SIZE + "&offset=" + offset))
      .done(function (data) {
        total = data.total;
        if (data.rows.length === 0 && offset > 0) { // last page emptied by deletes
          offset = Math.max(0, offset - PAGE_SIZE);
          return loadRows();
        }
        render(data.rows);
      })
      .fail(function (xhr) { showError(errorText(xhr)); });
  }

  function addRow($tr) {
    var payload = {};
    $tr.find("input").each(function () {
      var v = $(this).val();
      if (v !== "") payload[$(this).attr("data-col")] = v;
    });
    Admin.api("POST", rowsUrl(), payload)
      .done(function () { offset = 0; loadRows(); })
      .fail(function (xhr) { showError(errorText(xhr)); });
  }

  function editRow($tr) {
    var row = $tr.data("row");
    $tr.find("td[data-col]").each(function () {
      var $td = $(this);
      var col = $td.attr("data-col");
      if (col === current.primary_key) return;
      var v = row[col];
      $td.empty().append($("<input type='text'>").attr("data-col", col).val(v === null ? "" : v));
    });
    var $actions = $tr.find("td.actions").empty();
    $("<button>").text("Save").on("click", function () { saveRow($tr); }).appendTo($actions);
    $("<button class='secondary'>").text("Cancel").on("click", function () { $tr.replaceWith(dataTr(row)); }).appendTo($actions);
  }

  function saveRow($tr) {
    var row = $tr.data("row");
    var payload = {};
    $tr.find("input").each(function () { payload[$(this).attr("data-col")] = $(this).val(); });
    Admin.api("PUT", rowsUrl("/" + encodeURIComponent(row[current.primary_key])), payload)
      .done(function (updated) { showError(""); $tr.replaceWith(dataTr(updated)); })
      .fail(function (xhr) { showError(errorText(xhr)); });
  }

  function deleteRow(pk) {
    Admin.api("DELETE", rowsUrl("/" + encodeURIComponent(pk)))
      .done(loadRows)
      .fail(function (xhr) { showError(errorText(xhr)); });
  }

  // ---- wiring -------------------------------------------------------------

  $(function () {
    $("#table-select").on("change", function () { selectTable($(this).val()); });
    $("#db-refresh").on("click", loadRows);
    $("#db-prev").on("click", function () { offset = Math.max(0, offset - PAGE_SIZE); loadRows(); });
    $("#db-next").on("click", function () { offset += PAGE_SIZE; loadRows(); });
  });

  // Load the table list after login and whenever the tab is opened.
  $(document).on("admin:login", loadTables);
  $(document).on("admin:tab", function (_e, name) {
    if (name === "database" && current) loadRows();
  });
})(jQuery);
