/* Admin dashboard: login, tabs, health tab. */
window.Admin = (function ($) {
  var STORAGE_KEY = "admin_credentials";
  var API_BASE = ""; // same origin; set e.g. "http://127.0.0.1:8000" if served separately
  var authHeader = null;
  var loggedIn = false;

  // ---- auth ---------------------------------------------------------------

  function makeHeader(username, password) {
    // encodeURIComponent/unescape so non-latin1 characters survive btoa()
    return "Basic " + btoa(unescape(encodeURIComponent(username + ":" + password)));
  }

  // Every request to the API carries the stored credentials.
  $.ajaxSetup({
    beforeSend: function (xhr) {
      if (authHeader) xhr.setRequestHeader("Authorization", authHeader);
    }
  });

  // If the server stops accepting our credentials, go back to the login form.
  $(document).ajaxError(function (_e, xhr) {
    if (loggedIn && xhr.status === 401) logout("Session expired, please sign in again.");
  });

  function api(method, url, data) {
    var opts = { method: method, url: API_BASE + url, dataType: "json" };
    if (data !== undefined) {
      opts.contentType = "application/json";
      opts.data = JSON.stringify(data);
    }
    return $.ajax(opts);
  }

  function showLogin(message) {
    loggedIn = false;
    authHeader = null;
    $("#app-view").prop("hidden", true);
    $("#login-view").prop("hidden", false);
    $("#login-error").text(message || "").prop("hidden", !message);
    $("#login-password").val("").trigger("focus");
  }

  function showApp(username) {
    loggedIn = true;
    $("#login-view").prop("hidden", true);
    $("#app-view").prop("hidden", false);
    $("#whoami").text(username);
    checkHealth();
    $(document).trigger("admin:login");
  }

  function login(username, password) {
    authHeader = makeHeader(username, password);
    return api("POST", "/api/admin/login").done(function () {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ username: username, password: password }));
      showApp(username);
    }).fail(function () {
      authHeader = null;
    });
  }

  function logout(message) {
    localStorage.removeItem(STORAGE_KEY);
    showLogin(message);
  }

  // ---- tabs ---------------------------------------------------------------

  function selectTab(name) {
    $(".tab").removeClass("active").filter('[data-tab="' + name + '"]').addClass("active");
    $(".tab-panel").prop("hidden", true);
    $("#tab-" + name).prop("hidden", false);
    $(document).trigger("admin:tab", [name]);
  }

  // ---- health tab ---------------------------------------------------------

  function setStatus(text, cls) {
    $("#status").text(text).removeClass("ok error").addClass(cls || "");
  }

  function loadHistory() {
    api("GET", "/api/admin/health/history").done(function (rows) {
      var $body = $("#history tbody").empty();
      $.each(rows, function (_, r) {
        var $tr = $("<tr>");
        $("<td>").text(r.id).appendTo($tr);
        $("<td>").text(r.status).appendTo($tr);
        $("<td>").text(new Date(r.created_at + "Z").toLocaleString()).appendTo($tr);
        $body.append($tr);
      });
    });
  }

  function checkHealth() {
    api("GET", "/api/admin/health")
      .done(function (data) {
        setStatus(data.status, data.status === "ok" ? "ok" : "error");
        loadHistory();
      })
      .fail(function () {
        setStatus("unreachable", "error");
      });
  }

  // ---- startup ------------------------------------------------------------

  function start() {
    $("#login-form").on("submit", function (e) {
      e.preventDefault();
      $("#login-error").prop("hidden", true);
      login($("#login-username").val(), $("#login-password").val()).fail(function (xhr) {
        $("#login-error")
          .text(xhr.status === 401 ? "Invalid username or password." : "Could not reach the server.")
          .prop("hidden", false);
      });
    });
    $("#logout-btn").on("click", function () { logout(); });
    $(".tab").on("click", function () { selectTab($(this).data("tab")); });
    $("#check-btn").on("click", checkHealth);

    // Reuse stored credentials if they are still valid.
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch (e) {}
    if (saved && saved.username) {
      login(saved.username, saved.password).fail(function (xhr) {
        showLogin(xhr.status === 401 ? "Saved credentials are no longer valid." : "Could not reach the server.");
      });
    } else {
      showLogin();
    }
  }

  return { start: start, api: api };
})(jQuery);
