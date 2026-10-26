/* Admin dashboard: Visualizer tab. One canvas per iteration (0 = original). */
(function ($) {
  var generation = 0; // bumped on every reload so stale responses are ignored

  function showError(msg) {
    $("#viz-error").text(msg || "").prop("hidden", !msg);
  }

  function scale() { return parseInt($("#viz-scale").val(), 10); }

  // Paint one tile (32x32 symbols) at tile position (x, y) of a canvas.
  function paintTile(ctx, palette, tile, ts, s) {
    var img = ctx.createImageData(ts, ts);
    for (var i = 0; i < tile.pixels.length; i++) {
      var hex = palette[tile.pixels.charAt(i)] || "#ff00ff";
      img.data[i * 4] = parseInt(hex.substr(1, 2), 16);
      img.data[i * 4 + 1] = parseInt(hex.substr(3, 2), 16);
      img.data[i * 4 + 2] = parseInt(hex.substr(5, 2), 16);
      img.data[i * 4 + 3] = 255;
    }
    // draw at 1x on a scratch canvas, then scale up without smoothing
    var tmp = document.createElement("canvas");
    tmp.width = tmp.height = ts;
    tmp.getContext("2d").putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(tmp, tile.x * ts * s, tile.y * ts * s, ts * s, ts * s);
  }

  function hatch(ctx, grid, s, filled) {
    var ts = grid.tile_size * s;
    ctx.strokeStyle = "#9ca3af";
    ctx.lineWidth = 1;
    for (var y = 0; y < grid.rows; y++) {
      for (var x = 0; x < grid.columns; x++) {
        if (filled[y * grid.columns + x]) continue;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x * ts, y * ts, ts, ts);
        ctx.clip();
        ctx.beginPath();
        for (var k = -ts; k < ts; k += 8) {
          ctx.moveTo(x * ts + k, y * ts + ts);
          ctx.lineTo(x * ts + k + ts, y * ts);
        }
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  function renderIteration(meta, summary, data, $card, s) {
    var grid = summary.grid;
    var ts = grid.tile_size;
    var canvas = $card.find("canvas")[0];
    var ctx = canvas.getContext("2d");
    var filled = new Array(grid.columns * grid.rows);
    var info = {};
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    $.each(data.tiles, function (_, t) {
      paintTile(ctx, summary.palette, t, ts, s);
      filled[t.y * grid.columns + t.x] = true;
      info[t.y * grid.columns + t.x] = t;
    });
    hatch(ctx, grid, s, filled);

    var $hover = $card.find(".viz-hover");
    $(canvas).off("mousemove mouseleave").on("mousemove", function (e) {
      var r = canvas.getBoundingClientRect();
      var x = Math.floor((e.clientX - r.left) / (ts * s));
      var y = Math.floor((e.clientY - r.top) / (ts * s));
      var t = info[y * grid.columns + x];
      var text = "tile (" + x + ", " + y + ")";
      if (!t) text += ": not submitted";
      else if (meta.iteration > 0) {
        text += ": " + t.username + ", accuracy " + (t.accuracy * 100).toFixed(1) + "%";
      }
      $hover.text(text);
    }).on("mouseleave", function () { $hover.text(""); });
  }

  function load() {
    var gen = ++generation;
    var mode = $("#viz-mode").val();
    var s = scale();
    showError("");
    $("#viz-grid").empty();
    Admin.api("GET", "/api/admin/visualizer?game_type=" + mode).done(function (summary) {
      if (gen !== generation) return;
      var grid = summary.grid;
      $("#viz-info").text(summary.iterations.length + " iteration(s)");
      var queue = [];
      $.each(summary.iterations, function (_, meta) {
        var $card = $("<div class='viz-card'>");
        $("<h3>").text(meta.iteration === 0
          ? "Iteration 0 (original)"
          : "Iteration " + meta.iteration + " (" + meta.filled_tiles + "/" + summary.total_tiles + " tiles)")
          .appendTo($card);
        $("<canvas>").attr({ width: grid.columns * grid.tile_size * s,
                             height: grid.rows * grid.tile_size * s }).appendTo($card);
        $("<div class='viz-hover'>").appendTo($card);
        $("#viz-grid").append($card);
        queue.push({ meta: meta, $card: $card });
      });
      // Load iterations one after another, newest data last.
      (function next() {
        var item = queue.shift();
        if (!item || gen !== generation) return;
        Admin.api("GET", "/api/admin/visualizer/iteration?game_type=" + mode +
                  "&iteration=" + item.meta.iteration).done(function (data) {
          if (gen !== generation) return;
          renderIteration(item.meta, summary, data, item.$card, s);
          next();
        }).fail(function () { showError("Failed to load iteration " + item.meta.iteration); });
      })();
    }).fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  $(function () {
    $("#viz-mode, #viz-scale").on("change", load);
    $("#viz-refresh").on("click", load);
  });
  $(document).on("admin:tab", function (_e, name) {
    if (name === "visualizer") load();
  });
})(jQuery);
