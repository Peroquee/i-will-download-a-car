/* Sign-in, the account menu and the accounts page (issue #44): vanilla JS, no libraries, no
   external requests. Everything that comes from the server is set with textContent. The state is
   `document.body.dataset.auth` ("unknown", "in", "out") and `.role` ("admin", "user"); the style
   sheet hides what a visitor may not use. The server decides all the same (every route checks). */
(function (root) {
  "use strict";

  var MIN_PASSWORD = 12;
  var MAX_COOKIE_BYTES = 1024 * 1024;   // the server's limit for the cookies file (#46)

  function create(ctx) {
    var t = ctx.t;
    var $ = ctx.$;
    var account = null;
    var state = "unknown";
    var accountList = null;
    var setupNeeded = false;
    var accountsFailed = false;
    var confirmDelete = {};     // id -> true after the first click on Delete
    var resetOpen = {};         // id -> true while the "set password" field is shown
    var rowMessages = {};       // id -> {text(), ok}
    var cookieStatus = null;    // GET /api/youtube/cookies (#46)
    var cookiesFailed = false;

    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    }

    /* ---------------------------------------------------------------- who is asking */

    function setState(next, why) {
      account = next;
      state = next ? "in" : "out";
      document.body.setAttribute("data-auth", state);
      if (next) document.body.setAttribute("data-role", next.role);
      else document.body.removeAttribute("data-role");
      if (state === "out") {
        var note = $("login-note");
        note.textContent = why ? t(why) : "";
        note.hidden = !why;
      }
      renderTools();
      renderAccountResolution();
    }

    function check() {
      return fetch("/api/auth/me", {
        credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" },
      }).then(function (response) {
        if (response.status === 200) return response.json().then(function (data) { return data.account; });
        if (response.status === 401) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            return { setup: !!(data && data.setup) };
          });
        }
        throw new Error("status " + response.status);
      }).then(function (found) {
        if (found && found.setup !== undefined) {   // 401: nobody is signed in
          setState(null);
          showSetup(found.setup);
          return null;
        }
        setState(found || null);
        return found || null;
      }).catch(function () {
        setState(null, "login.network");
        return null;
      });
    }

    /* No account exists at all (the server says so): a form would be a dead end. */
    function showSetup(needed) {
      $("login-setup").hidden = !needed;
      $("login-setup").textContent = needed ? t("login.setup") : "";
      $("login-form").hidden = !!needed;
      setupNeeded = !!needed;
    }

    /* A request answered 401 while the page thought it was signed in: the session ended. */
    function expired() {
      if (state !== "in") return;
      setState(null, "login.expired");
      focusLogin();
    }

    function isIn() { return state === "in"; }
    function isAdmin() { return !!account && account.role === "admin"; }

    /* The admin's resolution right (#65): admins may always choose, everyone else only when the
       admin ticked it. A missing field (an old cached page) means the right is there. */
    function canChooseResolution() {
      return !!account && (account.role === "admin" || account.can_set_resolution !== false);
    }

    function focusLogin() { if ($("login-user")) $("login-user").focus(); }

    /* ---------------------------------------------------------------- the sign-in form */

    function loginError(key, vars) { $("login-error").textContent = key ? t(key, vars) : ""; }

    function onLogin(event) {
      event.preventDefault();
      loginError("");
      var name = $("login-user").value.trim();
      var password = $("login-password").value;
      if (!name || !password) {
        loginError("login.required");
        return;
      }
      $("login-submit").disabled = true;
      ctx.post("/api/auth/login", { username: name, password: password }).then(function (reply) {
        $("login-submit").disabled = false;
        if (reply.status === 200) {
          $("login-password").value = "";
          root.location.reload();   // a fresh page, started as this account
          return;
        }
        $("login-password").value = "";
        if (reply.status === 401) loginError("login.wrong");
        else if (reply.status === 429) loginError("login.wait", { n: reply.data.retry_after || 30 });
        else loginError("login.failed");
        $("login-password").focus();
      }).catch(function () {
        $("login-submit").disabled = false;
        loginError("login.network");
      });
    }

    /* ---------------------------------------------------------------- the account menu */

    function renderTools() {
      var button = $("account-open");
      if (!button) return;
      button.textContent = account ? account.username.charAt(0).toUpperCase() : "";
      button.setAttribute("aria-label", account
        ? t("account.menu") + ": " + account.username
        : t("account.menu"));
      if (account) {
        $("account-who").textContent = t("account.signed_in", {
          name: account.username, role: t("role." + account.role),
        });
      }
    }

    function accountMessage(text, ok) {
      $("account-result").textContent = text;
      $("account-result").className = "result " + (ok ? "ok" : "bad");
    }

    function openAccount() {
      accountMessage("", true);
      var dialog = $("account");
      renderAccountResolution();
      if (typeof dialog.showModal === "function") {
        if (!dialog.open) dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
    }

    function closeAccount() {
      var dialog = $("account");
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }

    /* Fill a resolution <select> from the choices the server sends (#75). The empty value means
       the server default. `value` is selected after the options exist. */
    function fillResolution(select, value) {
      var choices = typeof ctx.resolutionChoices === "function" ? ctx.resolutionChoices() : [];
      select.textContent = "";
      [["", "accounts.resolution_default"]]
        .concat(choices.map(function (height) {
          return [String(height), height === 0 ? "yt.resolution_best" : "yt.resolution_" + height];
        }))
        .forEach(function (choice) {
          var option = el("option", null, t(choice[1]));
          option.value = choice[0];
          select.appendChild(option);
        });
      select.value = value;
    }

    function resolutionValue(row) {
      return row && row.default_max_height != null ? String(row.default_max_height) : "";
    }

    /* The own default resolution (#65): only an account with the right sees the field; an admin
       always has it. */
    function renderAccountResolution() {
      var row = $("account-resolution-row");
      if (!row) return;
      row.hidden = !canChooseResolution();
      var select = $("account-resolution");
      if (!select) return;
      fillResolution(select, resolutionValue(account));
    }

    /* The resolution choices arrived after the page was built (#75, #109 review): refill only
       the resolution selects, never whole rows or dialogs, so nothing typed is lost. */
    function refreshResolutionSelects() {
      renderAccountResolution();
      (accountList || []).forEach(function (row) {
        var select = $("accounts-resolution-" + row.id);
        if (select) fillResolution(select, resolutionValue(row));
      });
    }

    function onChangeResolution() {
      var select = $("account-resolution");
      if (!select || !account) return;
      var value = select.value === "" ? null : Number(select.value);
      accountMessage("", true);
      ctx.request("PATCH", "/api/me/settings", { default_max_height: value }).then(function (reply) {
        if (reply.status === 200) {
          account = reply.data.account;
          renderAccountResolution();
          accountMessage(t("account.resolution_saved"), true);
        } else if (reply.status === 403) {
          accountMessage(t("account.resolution_forbidden"), false);
        } else {
          accountMessage(t("account.resolution_failed"), false);
        }
      }).catch(function () {
        accountMessage(t("account.resolution_failed") + " " + t("result.network"), false);
      });
    }

    function onChangePassword(event) {
      event.preventDefault();
      var currentPw = $("account-current").value;
      var next = $("account-new").value;
      if (!currentPw || !next) { accountMessage(t("login.required"), false); return; }
      if (next !== $("account-repeat").value) { accountMessage(t("account.mismatch"), false); return; }
      if (next.length < MIN_PASSWORD) { accountMessage(t("account.too_short", { n: MIN_PASSWORD }), false); return; }
      $("account-change").disabled = true;
      ctx.post("/api/auth/password", { current: currentPw, "new": next }).then(function (reply) {
        $("account-change").disabled = false;
        if (reply.status === 200) {
          ["account-current", "account-new", "account-repeat"].forEach(function (id) { $(id).value = ""; });
          accountMessage(t("account.changed"), true);
        } else if (reply.status === 403) {
          accountMessage(t("account.wrong_current"), false);
        } else if (reply.status === 429) {
          accountMessage(t("login.wait", { n: 30 }), false);
        } else if (reply.status === 422) {
          accountMessage(t("account.too_short", { n: MIN_PASSWORD }), false);
        } else {
          accountMessage(t("account.change_failed"), false);
        }
      }).catch(function () {
        $("account-change").disabled = false;
        accountMessage(t("account.change_failed") + " " + t("result.network"), false);
      });
    }

    function onLogout() {
      var done = function () { root.location.reload(); };
      ctx.post("/api/auth/logout", {}).then(done, done);
    }

    /* ---------------------------------------------------------------- the accounts page (admin) */

    function rowNote(id, key, ok) { rowMessages[id] = { ok: ok, text: function () { return t(key); } }; }

    function failureKey(reply, fallback) {
      var kind = reply.data && reply.data.kind;
      if (kind === "last_admin") return "accounts.last_admin";
      if (kind === "exists") return "accounts.exists";
      if (kind === "invalid") {
        var detail = String(reply.data.detail || "");
        if (/name/.test(detail)) return "accounts.invalid_name";
        if (/password/.test(detail)) return "accounts.weak";
      }
      return fallback;
    }

    function loadAccounts() {
      return ctx.getJson("/api/accounts").then(function (data) {
        accountList = data.accounts;
        accountsFailed = false;
      }).catch(function () { accountsFailed = true; }).then(renderAccounts);
    }

    function change(id, body) {
      delete rowMessages[id];
      ctx.request("PATCH", "/api/accounts/" + id, body).then(function (reply) {
        if (reply.status !== 200) rowNote(id, failureKey(reply, "accounts.failed"), false);
      }).catch(function () { rowNote(id, "accounts.failed", false); }).then(loadAccounts);
    }

    function removeAccount(row) {
      if (!confirmDelete[row.id]) {
        confirmDelete[row.id] = true;
        renderAccounts();
        root.setTimeout(function () {
          if (confirmDelete[row.id]) { delete confirmDelete[row.id]; renderAccounts(); }
        }, 5000);
        return;
      }
      delete confirmDelete[row.id];
      delete rowMessages[row.id];
      ctx.request("DELETE", "/api/accounts/" + row.id, null).then(function (reply) {
        if (reply.status === 200 || reply.status === 404) {
          $("accounts-result").textContent = t("accounts.deleted_note");
          $("accounts-result").className = "result ok";
        } else {
          rowNote(row.id, failureKey(reply, "accounts.failed"), false);
        }
      }).catch(function () { rowNote(row.id, "accounts.failed", false); }).then(loadAccounts);
    }

    function saveReset(row, input) {
      var password = input.value;
      if (password.length < MIN_PASSWORD) { rowNote(row.id, "accounts.weak", false); renderAccounts(); return; }
      ctx.post("/api/accounts/" + row.id + "/password", { password: password }).then(function (reply) {
        if (reply.status === 200) { delete resetOpen[row.id]; rowNote(row.id, "accounts.reset_done", true); }
        else rowNote(row.id, failureKey(reply, "accounts.failed"), false);
      }).catch(function () { rowNote(row.id, "accounts.failed", false); }).then(loadAccounts);
    }

    function button(key, className, onClick) {
      var b = el("button", "ghost small" + (className ? " " + className : ""), t(key));
      b.type = "button";
      b.addEventListener("click", onClick);
      return b;
    }

    /* The resolution settings of one account (#65): the personal default and the right. An empty
       select value means "server default"; every change is sent at once (like the buttons). */
    function accountResolutionRow(row) {
      var wrap = el("div", "row account-resolution-row");
      var label = el("label", "field", t("accounts.resolution"));
      var select = el("select");
      select.id = "accounts-resolution-" + row.id;
      label.setAttribute("for", select.id);
      fillResolution(select, resolutionValue(row));
      select.addEventListener("change", function () {
        change(row.id, {
          default_max_height: select.value === "" ? null : Number(select.value),
        });
      });
      wrap.appendChild(label);
      wrap.appendChild(select);
      var allowed = el("label", "checkbox");
      var tick = el("input");
      tick.type = "checkbox";
      tick.id = "accounts-can-choose-" + row.id;
      tick.checked = row.can_set_resolution !== false;
      tick.addEventListener("change", function () {
        change(row.id, { can_set_resolution: tick.checked });
      });
      allowed.appendChild(tick);
      allowed.appendChild(el("span", null, t("accounts.can_choose")));
      wrap.appendChild(allowed);
      return wrap;
    }

    function accountRow(row) {
      var me = account && row.id === account.id;
      var li = el("li", "job" + (row.disabled ? " job-failed" : ""));
      var top = el("div", "job-top");
      top.appendChild(el("span", "job-name", row.username + (me ? " (" + t("accounts.you") + ")" : "")));
      top.appendChild(el("span", "job-status", t("role." + row.role) + (row.disabled ? " · " + t("accounts.disabled") : "")));
      li.appendChild(top);
      li.appendChild(el("div", "job-meta", row.last_login
        ? t("accounts.last_login", { when: ctx.timeText(row.last_login) || row.last_login })
        : t("accounts.never")));
      li.appendChild(accountResolutionRow(row));
      if (rowMessages[row.id]) {
        li.appendChild(el("p", rowMessages[row.id].ok ? "job-note" : "job-error", rowMessages[row.id].text()));
      }
      if (resetOpen[row.id]) {
        var field = el("div", "row");
        var input = el("input");
        input.type = "password";
        input.autocomplete = "new-password";
        input.setAttribute("aria-label", t("accounts.password"));
        input.placeholder = t("accounts.password");
        field.appendChild(input);
        li.appendChild(field);
        var save = el("div", "row-actions");
        save.appendChild(button("accounts.reset_save", "", function () { saveReset(row, input); }));
        save.appendChild(button("account.close", "", function () { delete resetOpen[row.id]; renderAccounts(); }));
        li.appendChild(save);
        return li;
      }
      var actions = el("div", "row-actions");
      actions.appendChild(button("accounts.reset", "", function () { resetOpen[row.id] = true; renderAccounts(); }));
      if (!me) {
        actions.appendChild(button(row.role === "admin" ? "accounts.make_user" : "accounts.make_admin", "",
          function () { change(row.id, { role: row.role === "admin" ? "user" : "admin" }); }));
        actions.appendChild(button(row.disabled ? "accounts.enable" : "accounts.disable", "",
          function () { change(row.id, { disabled: !row.disabled }); }));
        actions.appendChild(button(confirmDelete[row.id] ? "accounts.confirm" : "accounts.delete",
          "danger", function () { removeAccount(row); }));
      }
      li.appendChild(actions);
      return li;
    }

    function renderAccounts() {
      var list = $("accounts-list");
      if (!list) return;
      list.textContent = "";
      $("accounts-load-error").textContent = accountsFailed ? t("accounts.load_error") : "";
      (accountList || []).forEach(function (row) { list.appendChild(accountRow(row)); });
    }

    function accountsResult(text, ok) {
      $("accounts-result").textContent = text;
      $("accounts-result").className = "result " + (ok ? "ok" : "bad");
    }

    function onCreate(event) {
      event.preventDefault();
      accountsResult("", true);
      var name = $("accounts-name").value.trim();
      var password = $("accounts-password").value;
      if (!name || !password) { accountsResult(t("login.required"), false); return; }
      if (password.length < MIN_PASSWORD) { accountsResult(t("accounts.weak"), false); return; }
      $("accounts-add").disabled = true;
      ctx.post("/api/accounts", { username: name, password: password, role: $("accounts-role").value }).then(function (reply) {
        $("accounts-add").disabled = false;
        if (reply.status === 201) {
          $("accounts-name").value = "";
          $("accounts-password").value = "";
          accountsResult(t("accounts.created", { name: reply.data.account.username }), true);
          loadAccounts();
        } else {
          accountsResult(t(failureKey(reply, "accounts.failed")), false);
        }
      }).catch(function () {
        $("accounts-add").disabled = false;
        accountsResult(t("accounts.failed") + " " + t("result.network"), false);
      });
    }

    /* ---------------------------------------------------------------- the YouTube cookies (admin, #46) */

    function cookiesResult(text, ok) {
      var node = $("cookies-result");
      node.textContent = text;
      node.className = "result " + (ok ? "ok" : "bad");
    }

    function loadCookies() {
      return ctx.getJson("/api/youtube/cookies").then(function (data) {
        cookieStatus = data;
        cookiesFailed = false;
      }).catch(function () { cookiesFailed = true; }).then(renderCookies);
    }

    function renderCookies() {
      var status = $("cookies-status");
      if (!status) return;
      if (cookiesFailed) {
        status.textContent = t("cookies.load_error");
      } else if (cookieStatus && cookieStatus.present) {
        var when = ctx.timeText(cookieStatus.updated_at) || cookieStatus.updated_at;
        status.textContent = t("cookies.present", { n: cookieStatus.cookie_count, when: when });
      } else if (cookieStatus) {
        status.textContent = t("cookies.absent");
      } else {
        status.textContent = "";
      }
      var remove = $("cookies-remove");
      if (remove) remove.disabled = !(cookieStatus && cookieStatus.present);
    }

    function readCookieFile(file) {
      if (typeof file.text === "function") return file.text();
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () { resolve(String(reader.result || "")); };
        reader.onerror = function () { reject(reader.error); };
        reader.readAsText(file);
      });
    }

    function onSaveCookies() {
      var input = $("cookies-file");
      var file = input.files && input.files[0];
      if (!file) { cookiesResult(t("cookies.no_file"), false); return; }
      if (file.size > MAX_COOKIE_BYTES) { cookiesResult(t("cookies.too_large"), false); return; }
      readCookieFile(file).then(function (text) {
        return ctx.requestText("PUT", "/api/youtube/cookies", text);
      }).then(function (reply) {
        if (reply.status === 200) {
          cookieStatus = reply.data;
          cookiesFailed = false;
          $("cookies-file").value = "";
          cookiesResult(t("cookies.saved"), true);
          renderCookies();
        } else if (reply.status === 400) {
          cookiesResult(t("cookies.invalid"), false);
        } else {
          cookiesResult(t("cookies.failed"), false);
        }
      }).catch(function () {
        cookiesResult(t("cookies.failed") + " " + t("result.network"), false);
      });
    }

    function onRemoveCookies() {
      ctx.request("DELETE", "/api/youtube/cookies", null).then(function (reply) {
        if (reply.status === 200) {
          cookieStatus = reply.data;
          cookiesFailed = false;
          cookiesResult(t("cookies.removed"), true);
          renderCookies();
        } else {
          cookiesResult(t("cookies.failed"), false);
        }
      }).catch(function () {
        cookiesResult(t("cookies.failed") + " " + t("result.network"), false);
      });
    }

    /* ---------------------------------------------------------------- wiring */

    function wire() {
      $("login-form").addEventListener("submit", onLogin);
      $("account-open").addEventListener("click", openAccount);
      $("account-close").addEventListener("click", closeAccount);
      $("account-form").addEventListener("submit", onChangePassword);
      $("account-resolution").addEventListener("change", onChangeResolution);
      $("account-logout").addEventListener("click", onLogout);
      $("account").addEventListener("click", function (event) { if (event.target === $("account")) closeAccount(); });
      $("accounts-form").addEventListener("submit", onCreate);
      $("cookies-save").addEventListener("click", onSaveCookies);
      $("cookies-remove").addEventListener("click", onRemoveCookies);
    }

    /* Called once the page knows who is signed in. */
    function start(view) {
      if (state !== "in") { focusLogin(); return; }
      if (view === "accounts") {
        if (!isAdmin()) { root.location.href = "/"; return; }
        loadAccounts();
        loadCookies();
      }
    }

    /* The language changed: texts built from data are made again. */
    function render() {
      renderTools();
      renderAccountResolution();
      if (setupNeeded) $("login-setup").textContent = t("login.setup");
      renderAccounts();
      renderCookies();
    }

    return {
      wire: wire, check: check, start: start, render: render, expired: expired,
      isIn: isIn, isAdmin: isAdmin, canChooseResolution: canChooseResolution,
      refreshResolutionSelects: refreshResolutionSelects,
    };
  }

  root.Auth = { create: create };
})(window);
