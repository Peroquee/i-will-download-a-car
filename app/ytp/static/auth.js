/* Sign-in, the account menu and the accounts page (issue #44): vanilla JS, no libraries, no
   external requests. Everything that comes from the server is set with textContent. The state is
   `document.body.dataset.auth` ("unknown", "in", "out") and `.role` ("admin", "user"); the style
   sheet hides what a visitor may not use. The server decides all the same (every route checks). */
(function (root) {
  "use strict";

  var MIN_PASSWORD = 12;

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

    function passwordMessage(text, ok) {
      $("account-result").textContent = text;
      $("account-result").className = "result " + (ok ? "ok" : "bad");
    }

    function openAccount() {
      passwordMessage("", true);
      var dialog = $("account");
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

    function onChangePassword(event) {
      event.preventDefault();
      var currentPw = $("account-current").value;
      var next = $("account-new").value;
      if (!currentPw || !next) { passwordMessage(t("login.required"), false); return; }
      if (next !== $("account-repeat").value) { passwordMessage(t("account.mismatch"), false); return; }
      if (next.length < MIN_PASSWORD) { passwordMessage(t("account.too_short", { n: MIN_PASSWORD }), false); return; }
      $("account-change").disabled = true;
      ctx.post("/api/auth/password", { current: currentPw, "new": next }).then(function (reply) {
        $("account-change").disabled = false;
        if (reply.status === 200) {
          ["account-current", "account-new", "account-repeat"].forEach(function (id) { $(id).value = ""; });
          passwordMessage(t("account.changed"), true);
        } else if (reply.status === 403) {
          passwordMessage(t("account.wrong_current"), false);
        } else if (reply.status === 429) {
          passwordMessage(t("login.wait", { n: 30 }), false);
        } else if (reply.status === 422) {
          passwordMessage(t("account.too_short", { n: MIN_PASSWORD }), false);
        } else {
          passwordMessage(t("account.change_failed"), false);
        }
      }).catch(function () {
        $("account-change").disabled = false;
        passwordMessage(t("account.change_failed") + " " + t("result.network"), false);
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

    /* ---------------------------------------------------------------- wiring */

    function wire() {
      $("login-form").addEventListener("submit", onLogin);
      $("account-open").addEventListener("click", openAccount);
      $("account-close").addEventListener("click", closeAccount);
      $("account-form").addEventListener("submit", onChangePassword);
      $("account-logout").addEventListener("click", onLogout);
      $("account").addEventListener("click", function (event) { if (event.target === $("account")) closeAccount(); });
      $("accounts-form").addEventListener("submit", onCreate);
    }

    /* Called once the page knows who is signed in. */
    function start(view) {
      if (state !== "in") { focusLogin(); return; }
      if (view === "accounts") {
        if (!isAdmin()) { root.location.href = "/"; return; }
        loadAccounts();
      }
    }

    /* The language changed: texts built from data are made again. */
    function render() {
      renderTools();
      if (setupNeeded) $("login-setup").textContent = t("login.setup");
      renderAccounts();
    }

    return {
      wire: wire, check: check, start: start, render: render, expired: expired,
      isIn: isIn, isAdmin: isAdmin,
    };
  }

  root.Auth = { create: create };
})(window);
