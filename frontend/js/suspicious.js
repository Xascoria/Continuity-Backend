/* Admin dashboard: Suspicious events tab. */
(function ($) {
  var PAGE_SIZE = 25;
  var offset = 0;
  var total = 0;
  var timer = null;
  var knownTypes = {};

  function showError(msg) {
    $("#sus-error").text(msg || "").prop("hidden", !msg);
  }

  function query() {
    var q = { limit: PAGE_SIZE, offset: offset };
    var map = { event_type: "#sus-type", severity: "#sus-severity",
                client_ip: "#sus-ip", user_id: "#sus-user" };
    $.each(map, function (key, sel) {
      var v = $.trim($(sel).val());
      if (v) q[key] = v;
    });
    return $.param(q);
  }

  function addTypes(types) {
    var $sel = $("#sus-type");
    $.each(types, function (_, t) {
      if (knownTypes[t]) return;
      knownTypes[t] = true;
      $("<option>").val(t).text(t).appendTo($sel);
    });
  }

  function renderSummary(counts) {
    var $box = $("#sus-summary").empty();
    var names = Object.keys(counts).sort();
    if (!names.length) $box.append($("<span class='muted'>").text("No events in the last 24 hours."));
    addTypes(names);
    $.each(names, function (_, name) {
      $("<button class='secondary'>").text(name + " (" + counts[name] + ")")
        .on("click", function () { $("#sus-type").val(name); offset = 0; load(); })
        .appendTo($box);
    });
  }

  function render(data) {
    total = data.total;
    renderSummary(data.last_24h);
    var $body = $("#sus-table tbody").empty();
    $.each(data.events, function (_, e) {
      var $tr = $("<tr>");
      $("<td>").text(e.id).appendTo($tr);
      $("<td>").text(new Date(e.created_at + "Z").toLocaleString()).appendTo($tr);
      $("<td>").text(e.event_type).appendTo($tr);
      $("<td>").append($("<span class='badge'>").addClass("sev-" + e.severity).text(e.severity)).appendTo($tr);
      $("<td>").text(e.client_ip || "").appendTo($tr);
      $("<td>").text(e.user_id === null ? "" : e.user_id).appendTo($tr);
      $("<td>").append($("<code>").text(JSON.stringify(e.details))).appendTo($tr);
      var $act = $("<td class='actions'>").appendTo($tr);
      if (e.user_banned !== null) {
        $("<button>").addClass(e.user_banned ? "secondary" : "danger")
          .text(e.user_banned ? "Unban User" : "Ban User")
          .on("click", function () { toggleUser(e); }).appendTo($act);
      }
      if (e.ip_banned !== null) {
        $("<button>").addClass(e.ip_banned ? "secondary" : "danger")
          .text(e.ip_banned ? "Unban IP" : "Ban IP")
          .on("click", function () { toggleIp(e); }).appendTo($act);
      }
      $body.append($tr);
    });
    var from = total ? offset + 1 : 0;
    $("#sus-info").text(from + "-" + (offset + data.events.length) + " of " + total);
    $("#sus-prev").prop("disabled", offset <= 0);
    $("#sus-next").prop("disabled", offset + PAGE_SIZE >= total);
  }

  function toggleUser(e) {
    var ban = !e.user_banned;
    if (ban && !confirm("Ban user " + e.user_id + "? Their token will be rejected on every call.")) return;
    Admin.api("POST", "/api/admin/users/" + e.user_id + (ban ? "/ban" : "/unban"))
      .done(load)
      .fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  function toggleIp(e) {
    var ban = !e.ip_banned;
    if (ban && !confirm("Ban IP " + e.client_ip + "? All its API requests will be refused (403).")) return;
    Admin.api("POST", "/api/admin/" + (ban ? "ban_ip" : "unban_ip"),
              { ip: e.client_ip, reason: ban ? "Banned from event #" + e.id : undefined })
      .done(load)
      .fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  function load() {
    showError("");
    Admin.api("GET", "/api/admin/suspicious_events?" + query())
      .done(render)
      .fail(function (xhr) { showError("Request failed (" + xhr.status + ")"); });
  }

  function setAuto(on) {
    clearInterval(timer);
    timer = on ? setInterval(function () {
      if (!$("#tab-suspicious").prop("hidden")) load();
    }, 10000) : null;
  }

  $(function () {
    $("#sus-refresh").on("click", load);
    $("#sus-type, #sus-severity").on("change", function () { offset = 0; load(); });
    $("#sus-ip, #sus-user").on("keydown", function (e) {
      if (e.key === "Enter") { offset = 0; load(); }
    });
    $("#sus-prev").on("click", function () { offset = Math.max(0, offset - PAGE_SIZE); load(); });
    $("#sus-next").on("click", function () { offset += PAGE_SIZE; load(); });
    $("#sus-auto").on("change", function () { setAuto($(this).prop("checked")); });
  });

  $(document).on("admin:tab", function (_e, name) {
    if (name === "suspicious") load();
  });
})(jQuery);
