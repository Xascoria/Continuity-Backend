/* Admin dashboard: Player activities tab (submitted_for_type_one). */
(function ($) {
  var PAGE_SIZE = 50;
  var offset = 0;
  var total = 0;
  var palette = {};
  var tileSize = 32;

  function showError(msg) {
    $("#act-error").text(msg || "").prop("hidden", !msg);
  }

  function paintTile(canvas, pixels) {
    var ts = tileSize;
    var scale = 8;
    canvas.width = canvas.height = ts * scale;
    var ctx = canvas.getContext("2d");
    var img = ctx.createImageData(ts, ts);
    var n = ts * ts;
    for (var i = 0; i < n; i++) {
      var hex = palette[pixels.charAt(i)] || "#ff00ff";
      img.data[i * 4] = parseInt(hex.substr(1, 2), 16);
      img.data[i * 4 + 1] = parseInt(hex.substr(3, 2), 16);
      img.data[i * 4 + 2] = parseInt(hex.substr(5, 2), 16);
      img.data[i * 4 + 3] = 255;
    }
    var tmp = document.createElement("canvas");
    tmp.width = tmp.height = ts;
    tmp.getContext("2d").putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(tmp, 0, 0, ts * scale, ts * scale);
  }

  function tileTitle(kind, row) {
    var where = row.x === null || row.y === null ? "" : " · tile (" + row.x + ", " + row.y + ")";
    if (kind === "source") {
      var from = row.source_iteration === null ? "" : " · from iteration " + row.source_iteration;
      return "Source tile · " + row.username + where + from;
    }
    return "Drawn tile · " + row.username + " · iteration " + row.iteration + where;
  }

  function showTile(title, pixels) {
    $("#tile-modal-title").text(title);
    paintTile($("#tile-modal-canvas")[0], pixels);
    $("#tile-modal").prop("hidden", false);
    $("#tile-modal-close").trigger("focus");
  }

  function closeTile() {
    $("#tile-modal").prop("hidden", true);
  }

  function viewButton(pixels, title) {
    var $btn = $("<button type='button' class='secondary'>").text(pixels ? "View" : "Unavailable");
    if (!pixels) return $btn.prop("disabled", true);
    return $btn.on("click", function () { showTile(title, pixels); });
  }

  function render(data) {
    total = data.total;
    palette = data.palette || {};
    tileSize = data.tile_size || 32;
    var $body = $("#act-table tbody").empty();
    if (!data.activities.length) {
      $("<tr>").append($("<td class='muted' colspan='6'>").text("No submissions yet.")).appendTo($body);
    }
    $.each(data.activities, function (_, row) {
      var $tr = $("<tr>");
      $("<td>").text(row.username).appendTo($tr);
      $("<td>").text(row.submitted_time).appendTo($tr);
      $("<td>").text(row.submissions_so_far).appendTo($tr);
      $("<td>").text(row.iteration).appendTo($tr);
      $("<td class='actions'>").append(viewButton(row.source_tile, tileTitle("source", row))).appendTo($tr);
      $("<td class='actions'>").append(viewButton(row.drawn_tile, tileTitle("drawn", row))).appendTo($tr);
      $body.append($tr);
    });
    var from = total ? offset + 1 : 0;
    $("#act-info").text(from + "-" + (offset + data.activities.length) + " of " + total);
    $("#act-prev").prop("disabled", offset <= 0);
    $("#act-next").prop("disabled", offset + PAGE_SIZE >= total);
  }

  function load() {
    showError("");
    Admin.api("GET", "/api/admin/player_activities?limit=" + PAGE_SIZE + "&offset=" + offset)
      .done(render)
      .fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  $(function () {
    $("#act-refresh").on("click", load);
    $("#act-prev").on("click", function () { offset = Math.max(0, offset - PAGE_SIZE); load(); });
    $("#act-next").on("click", function () { offset += PAGE_SIZE; load(); });
    $("#tile-modal-close").on("click", closeTile);
    $("#tile-modal").on("click", function (e) {
      if (e.target === this) closeTile();
    });
    $(document).on("keydown", function (e) {
      if (e.key === "Escape") closeTile();
    });
  });

  $(document).on("admin:tab", function (_e, name) {
    if (name === "activities") load();
  });
})(jQuery);
