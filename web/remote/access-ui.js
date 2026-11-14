/* Account and room-policy forms. All mutations use the shared session boundary. */
(function (global) {
  "use strict";
  const client = global.ThermiteAccess.client, page = document.body.dataset.thermitePage;
  const $ = (selector) => document.querySelector(selector), house = global.REMOTE_HOUSE;
  const status = (message, bad) => { const item = $("[data-access-status]"); item.textContent = message || ""; item.dataset.tone = bad ? "bad" : ""; };
  const node = (tag, text, attrs) => {
    const item = document.createElement(tag); if (text != null) item.textContent = text;
    Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, String(value))); return item;
  };
  function button(label, action) { const item = node("button", label, { type: "button" }); item.addEventListener("click", action); return item; }
  function input(label, name, type, value) {
    const box = node("label", label), field = node("input", null, { name, type: type || "text" });
    if (type === "checkbox") field.checked = Boolean(value); else field.value = value == null ? "" : value;
    box.appendChild(field); return box;
  }
  function select(label, name, choices, value) {
    const box = node("label", label), field = node("select", null, { name });
    choices.forEach(([id, text]) => field.appendChild(node("option", text, { value: id })));
    field.value = String(value == null ? choices[0]?.[0] || "" : value); box.appendChild(field); return box;
  }
  function formError(form, message) {
    let note = form.querySelector('[role="alert"]');
    if (!note) { note = node("p", null, { role: "alert" }); form.prepend(note); }
    note.textContent = message;
  }
  function dialog(title, build) {
    const item = node("dialog", null, { class: "access-dialog" }); item.appendChild(node("h2", title));
    const form = node("form", null, { class: "access-form" }), actions = node("div", null, { class: "access-actions" });
    const save = node("button", "Save", { type: "submit" }); actions.append(save, button("Cancel", () => item.close()));
    item.appendChild(form); build(form, item); form.appendChild(actions); document.body.appendChild(item);
    item.addEventListener("close", () => { item.querySelectorAll('input[type="password"]').forEach((field) => { field.value = ""; }); item.remove(); }); item.addEventListener("keydown", (event) => event.stopPropagation()); item.showModal(); return { item, form, save };
  }
  async function login() {
    const form = $("[data-login]");
    const mode = await client.loadSecurity();
    const signOut = () => client.logout().then(() => location.reload()).catch((error) => status(error.message, true));
    if (!mode.enabled) {
      $(".access-login h1").textContent = "Authentication is off"; form.replaceChildren();
      form.append(node("p", "Basic remote controls are available without signing in. Enable authentication for accounts, device pairing, locks and room schedules."),
        node("a", "Set up authentication in Security", { href: global.ThermiteNavigation.href("security", location.search) }));
      try { await client.ready(); status(""); }
      catch (error) { status(error.message, true); if (error.status === 401) form.appendChild(button("Clear expired sign-in", signOut)); }
      return;
    }
    try {
      const user = await client.ready();
      if (user && !user.is_guest) {
        $(".access-login h1").textContent = "Your account"; form.replaceChildren();
        form.append(node("p", "Signed in as " + user.username), node("p", "To elevate access, switch to an administrator account. An administrator can make your account an administrator in Users; then sign in again."),
          button("Switch account", signOut), button("Sign out", signOut));
        status(user.can_edit ? "Administrator" : "Account · unlocked rooms and camera media"); return;
      }
    } catch (error) {
      if (error.status === 401) form.appendChild(button("Clear expired sign-in", signOut));
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const username = form.elements.username.value, password = form.elements.password.value;  // lint: allow — reads the password field; no embedded credential
      form.elements.password.value = ""; const submit = form.querySelector("button"); submit.disabled = true; status("Signing in…");
      try {
        await client.login(username, password);
        location.replace(global.ThermiteAccess.returnTo(new URLSearchParams(location.search).get("next"), location.origin));
      } catch (error) { status(error.message, true); form.elements.password.focus(); }
      finally { submit.disabled = false; }
    });
    status(client.current?.()?.is_guest ? "Unlocked rooms work without signing in. Sign in to view cameras or, as an administrator, to manage the site." : "");
  }
  // Whether Guests see free text in the activity feed; an Admin's choice
  // while authentication is on, when there is anyone else to see it.
  function guests(mode) {
    const host = $("[data-security-guests]");
    if (!host) return;
    host.replaceChildren();
    if (!mode.enabled) return;
    host.append(node("h2", "What guests see"), node("p",
      "Guests, who use the remotes without signing in, see what each room is playing in the remotes and the activity feed: titles, artists, apps and playback. Accounts always see it. This does not cover the Grafana dashboards, which keep what played and which anyone on your network can open."));
    if (client.mock || !client.canEdit()) {
      host.appendChild(node("p", mode.guest_text ? "Guests see it." : "Guests do not see it.")); return;
    }
    const form = node("form", null, { class: "access-form" }), box = input("Guests see titles, artists and apps", "guest_text", "checkbox", mode.guest_text);
    const field = box.firstElementChild;
    form.addEventListener("submit", (event) => event.preventDefault());
    field.addEventListener("change", async () => {
      field.disabled = true; status("Saving what guests see…");
      try { field.checked = (await client.setGuestText(field.checked)).guest_text; status(""); }
      catch (error) { field.checked = !field.checked; status(error.message, true); }
      finally { field.disabled = false; }
    });
    form.appendChild(box); host.appendChild(form);
  }
  async function security() {
    const host = $("[data-security]");
    const mode = await client.loadSecurity();
    if (mode.enabled) { try { await client.ready(); } catch (_) { /* The public Security page also offers sign-in. */ } }
    status("");
    host.replaceChildren();
    guests(mode);
    host.append(node("h2", "Authentication is " + (mode.enabled ? "on" : "off")), node("p", mode.enabled
      ? "Unlocked rooms work without signing in. Accounts can view camera media; administrators manage locks, schedules, users, pairing and the system."
      : "Basic remote controls are available without signing in. Accounts, device pairing, locks and room schedules require authentication."));
    host.appendChild(node("p", "Changing this setting signs out every browser and terminal. Existing credentials cannot be reused after a change.", { class: "access-note" }));
    if (client.mock) { host.appendChild(node("p", "Demo authentication cannot be changed.")); return; }
    if (mode.enabled && !client.canEdit()) {
      const account = global.ThermiteNavigation.href("access", location.search);
      host.append(node("p", "Sign in as an administrator to change authentication. To elevate an existing account, ask an administrator to make it an administrator in Users, then sign in again."),
        node("a", client.current() && !client.current().is_guest ? "Switch to an administrator account" : "Sign in as an administrator", { href: account + (account.includes("?") ? "&" : "?") + "next=%2Fsecurity%2F" }));
      return;
    }
    if (mode.bootstrap_required) {
      host.append(node("p", "Complete the HTTPS gateway setup and restrict direct runtime access before relying on accounts or room locks."),
        node("p", "Create your first administrator here. On the thermite host, generate a short-lived setup code for this site:"),
        node("code", "thermite access setup --config /path/to/access/config.json"),
        node("p", "Run the staged thermite release as the thermite service account, using its access configuration.", { class: "access-note" }),
        node("p", mode.setup_available ? "A setup code is ready. Enter it below." : "No setup code is ready. Run the command, then enter the code below.", { class: "access-note" }));
    }
    const form = node("form", null, { class: "access-form" });
    if (!mode.enabled) {
      const username = input(mode.bootstrap_required ? "New administrator username" : "Administrator username", "username");
      Object.assign(username.firstElementChild, { required: true, maxLength: 150, autocomplete: "username" }); form.appendChild(username);
    }
    const password = input(mode.bootstrap_required ? "New administrator password" : "Administrator password", "password", "password");  // lint: allow — credential field label and type
    Object.assign(password.firstElementChild, { required: true, maxLength: 1024, autocomplete: mode.bootstrap_required ? "new-password" : "current-password" }); form.appendChild(password);
    if (mode.bootstrap_required) {
      password.firstElementChild.minLength = 12;
      const confirm = input("Confirm new administrator password", "confirm_password", "password");
      Object.assign(confirm.firstElementChild, { required: true, minLength: 12, maxLength: 1024, autocomplete: "new-password" }); form.appendChild(confirm);
      const setup = input("Host setup code", "setup_code", "password");
      Object.assign(setup.firstElementChild, { required: true, maxLength: 200, autocomplete: "off" }); form.appendChild(setup);
    }
    const save = node("button", mode.enabled ? "Disable authentication" : "Enable authentication", { type: "submit" });
    form.appendChild(save); host.appendChild(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (mode.bootstrap_required && form.elements.password.value !== form.elements.confirm_password.value) {
        status("The new passwords do not match.", true); form.elements.confirm_password.focus(); return;
      }
      save.disabled = true;
      const value = { enabled: !mode.enabled, revision: mode.revision, password: form.elements.password.value };  // lint: allow — entered password only
      if (!mode.enabled) value.username = form.elements.username.value;
      if (mode.bootstrap_required) value.setup_code = form.elements.setup_code.value;
      form.querySelectorAll('input[type="password"]').forEach((field) => { field.value = ""; }); status("Changing authentication…");
      try {
        const saved = await client.setSecurity(value);
        if (saved.enabled) location.replace("/account/?next=%2Fsecurity%2F");
        else location.reload();
      } catch (error) {
        if (error.code === "revision_conflict") await security();
        status(error.code === "revision_conflict" ? "Authentication changed elsewhere. Review the current setting before trying again." : error.message, true);
      } finally { save.disabled = false; }
    });
  }
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  // Day toggles: a real checkbox per day, shown as a chip.
  function days(values) {
    const field = node("fieldset", null, { class: "access-days" }); field.appendChild(node("legend", "Days", { class: "sr-only" }));
    DAYS.forEach((name, id) => {
      const chip = node("label", null, { class: "access-day" }), box = node("input", null, { type: "checkbox", name: "day", value: id });
      box.checked = values.includes(id); chip.append(box, node("span", name)); field.appendChild(chip);
    });
    return field;
  }
  const selectedDays = (row) => Array.from(row.querySelectorAll('[name="day"]:checked'), (item) => Number(item.value));
  const clock = (value) => { const at = new Date(value); return Number.isFinite(at.getTime()) ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""; };
  let turn = 0, revision = 0, currentRoom = null, serial = 0;
  async function policy() {
    const selector = $("[data-policy-room]");
    house.rooms.forEach((room) => selector.appendChild(node("option", room.label, { value: room.id })));
    let requested = new URLSearchParams(location.search).get("room");
    if (!requested) { try { requested = localStorage.getItem("remote.room"); } catch (_) {} }
    if (house.rooms.some((room) => room.id === requested)) selector.value = requested;
    selector.addEventListener("change", () => {
      try { const address = new URL(location.href); address.searchParams.set("room", selector.value); history.replaceState(null, "", address); } catch (_) {}
      readPolicy();
    });
    await readPolicy();
  }
  async function readPolicy() {
    const current = ++turn, room = $("[data-policy-room]").value; currentRoom = room; status("Reading schedule…");
    try { const answer = await client.loadPolicy(room); if (current !== turn) return; revision = answer.revision; drawPolicy(answer); status(""); }
    catch (error) { if (current === turn) { $("[data-policy]").replaceChildren(); status(error.message, true); } }
  }
  // One line for what the room does now; detail only while it is locked.
  function policyStatus(answer, config) {
    const effective = answer.effective, box = node("div", null, { class: "policy-status" });
    const source = (id) => id === "off" ? "Off" : house.sources[id]?.label || "an unavailable source";
    let headline = "Status not reported", detail = [];
    if (effective?.locked) {
      headline = answer.manual_lock ? "Locked until an administrator unlocks it" : "Locked by schedule" + (effective.until && clock(effective.until) ? " until " + clock(effective.until) : "");
      if (effective.desired) detail.push("Holds " + source(effective.desired));
      detail.push(({ verified: "verified", degraded: "needs attention", unknown: "awaiting verification" })[answer.enforcement?.state] || "verification not reported");
    } else if (effective) headline = config.enabled ? "Unlocked · schedule on" : "Unlocked · no schedule";
    box.dataset.tone = effective?.locked ? "locked" : "";
    box.appendChild(node("strong", headline));
    if (detail.length) box.appendChild(node("span", detail.join(" · ")));
    return box;
  }
  function drawPolicy(answer) {
    const room = house.rooms.find((entry) => entry.id === currentRoom), editable = answer.can_edit === true && client.canEdit();
    const config = answer.config || { version: 1, enabled: false, daily_off: [], locks: [] };
    const host = $("[data-policy]"); host.replaceChildren(policyStatus(answer, config));
    if (!editable) host.appendChild(node("p", "Only administrators can change this schedule.", { class: "access-readonly" }));
    const form = node("form", null, { class: "policy-form" }), fields = node("fieldset", null, { class: "policy-fields" }); fields.disabled = !editable;
    const enabled = input("Use a schedule", "enabled", "checkbox", config.enabled); enabled.className = "access-check access-switch";
    enabled.firstElementChild.setAttribute("role", "switch");
    fields.append(enabled, node("p", "Times use " + (config.timezone || "the site's timezone") + ". A lock that ends earlier than it starts runs overnight.", { class: "access-note" }));
    const sources = [["off", "Off"], ...room.sources.map((id) => [id, house.sources[id]?.label || id])];
    const ruleIds = new Set([...(config.daily_off || []), ...(config.locks || [])].map((rule) => rule.id));
    const groups = {};
    function group(kind, title, addLabel) {
      const section = node("section", null, { class: "policy-group" }), head = node("div", null, { class: "policy-group__head" });
      const list = node("ol", null, { class: "policy-rules" }), empty = node("p", kind === "locks" ? "No lock windows." : "No daily off times.", { class: "policy-empty" });
      head.append(node("h2", title), button(addLabel, () => add(kind, {})));
      section.append(head, list, empty); fields.appendChild(section);
      groups[kind] = { list, empty };
    }
    const refresh = (kind) => { groups[kind].empty.hidden = groups[kind].list.children.length > 0; };
    function add(kind, value) {
      const row = node("li", null, { class: "policy-rule" + (kind === "locks" ? " policy-rule--lock" : ""), "data-rule": kind });
      if (value.id) row.dataset.id = value.id;
      else { do { row.dataset.id = "rule-" + (++serial); } while (ruleIds.has(row.dataset.id)); ruleIds.add(row.dataset.id); }
      const times = node("div", null, { class: "policy-times" });
      if (kind === "daily_off") times.appendChild(input("Turn off at", "time", "time", value.time || "22:00"));
      else times.append(input("From", "start", "time", value.start || "23:00"), input("Until", "end", "time", value.end || "07:00"),
        select("Hold", "desired", sources, value.desired || "off"), select("Afterwards", "unlock", [["leave", "Leave as is"], ...sources], value.unlock || "leave"));
      const remove = button("×", () => { row.remove(); refresh(kind); });
      remove.className = "policy-remove"; remove.setAttribute("aria-label", kind === "locks" ? "Remove this lock window" : "Remove this daily off time");
      row.append(times, days(value.days || [0, 1, 2, 3, 4, 5, 6]), remove);
      groups[kind].list.appendChild(row); refresh(kind);
    }
    group("daily_off", "Daily off", "Add time"); (config.daily_off || []).forEach((value) => add("daily_off", value)); refresh("daily_off");
    group("locks", "Lock windows", "Add window"); (config.locks || []).forEach((value) => add("locks", value)); refresh("locks");
    form.appendChild(fields);
    const save = node("button", "Save schedule", { type: "submit", class: "policy-save" }); save.hidden = !editable; form.appendChild(save); host.appendChild(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (!editable) return;
      const value = { version: 1, enabled: fields.querySelector('[name="enabled"]').checked, daily_off: [], locks: [] };
      fields.querySelectorAll("[data-rule]").forEach((row) => {
        const kind = row.dataset.rule, rule = { id: row.dataset.id, days: selectedDays(row) };
        (kind === "locks" ? ["start", "end", "desired", "unlock"] : ["time"]).forEach((key) => { rule[key] = row.querySelector('[name="' + key + '"]').value; }); value[kind].push(rule);
      });
      save.disabled = true; const roomId = currentRoom, generation = turn;
      try { const saved = await client.savePolicy(roomId, revision, value); if (turn === generation) { revision = saved.revision; status("Schedule saved."); } }
      catch (error) {
        if (turn !== generation) return;
        // Someone else changed it: show the current schedule, then say why.
        if (error.code === "revision_conflict") await readPolicy();
        status(error.message, true);
      }
      finally { if (turn === generation) save.disabled = false; }
    });
  }
  async function users() {
    if (!client.canEdit()) { status("Only administrators can manage users. Switch to an administrator account.", true); return; }
    let userList = [];
    async function reload() { userList = (await client.json("/api/v1/users")).users; draw(); status(""); }
    function draw() {
      const section = $("[data-users]"); section.replaceChildren();
      const bar = node("div", null, { class: "access-toolbar" }); bar.append(node("h2", "Users"), button("Add user", () => editUser())); section.appendChild(bar);
      section.appendChild(node("p", "Anyone can use unlocked rooms without an account. Accounts can also view camera media; administrators manage locks, schedules, users, pairing and the system.", { class: "access-note" }));
      const table = node("table", null, { class: "access-table" }), head = node("thead"), titles = node("tr"), body = node("tbody");
      ["Username", "Access", "Status", "Actions"].forEach((title) => titles.appendChild(node("th", title, { scope: "col" }))); head.appendChild(titles); table.append(head, body);
      userList.forEach((user) => { const row = node("tr");
        [user.username, user.admin ? "Administrator" : "Account", user.active ? "Active" : "Disabled"].forEach((value) => row.appendChild(node("td", value)));
        const actions = node("td"); actions.appendChild(button("Edit", () => editUser(user))); row.appendChild(actions); body.appendChild(row);
      }); const scroll = node("div", null, { class: "access-table-wrap" }); scroll.appendChild(table); section.appendChild(scroll);
    }
    function editUser(user) {
      const existing = user || { username: "", admin: false, active: true };
      dialog(user ? "Edit user" : "Add user", (form, modal) => {
        const username = input("Username", "username", "text", existing.username); username.firstElementChild.required = true; username.firstElementChild.maxLength = 150; username.firstElementChild.autocomplete = "off";
        const secret = input(user ? "New password (leave blank to keep)" : "Password", "password", "password"); secret.firstElementChild.autocomplete = "new-password"; secret.firstElementChild.maxLength = 1024; secret.firstElementChild.required = !user;  // lint: allow — password field labels and input type
        const admin = input("Administrator", "admin", "checkbox", existing.admin); admin.className = "access-check";
        const active = input("Active account", "active", "checkbox", existing.active); active.className = "access-check";
        form.append(username, secret, admin, node("p", "Changing administration signs this user out on every browser and terminal; they must sign in again.", { class: "access-note" }), active);
        form.addEventListener("submit", async (event) => { event.preventDefault();
          const value = { username: form.elements.username.value, admin: form.elements.admin.checked, active: form.elements.active.checked };
          if (form.elements.password.value) value.password = form.elements.password.value; form.elements.password.value = "";  // lint: allow — copies entered data, then clears the field
          const save = form.querySelector('[type="submit"]'); save.disabled = true;
          try { await client.json("/api/v1/users" + (user ? "/" + user.id : ""), user ? "PATCH" : "POST", value); modal.close(); await reload(); }
          catch (error) { formError(form, error.message); }
          finally { save.disabled = false; }
        });
      });
    }
    await reload();
  }
  if (client.native) {
    document.querySelectorAll("form, [data-policy-room], [data-policy-reload], .access-toolbar").forEach((item) => { item.hidden = true; });
    document.querySelectorAll(".access-login > p:not([data-access-status])").forEach((item) => { item.hidden = true; });
    status("Authentication cannot be changed in native testing mode. Configure and open the thermite gateway, then use Security to enable accounts, pairing, locks and schedules.");
    return;
  }
  if (page === "access") login().catch((error) => status(error.message, true));
  else if (page === "security") security().catch((error) => status(error.message, true));
  else client.ready().then(() => {
    if (!client.authEnabled()) {
      document.querySelectorAll("form, .settings-room, .access-toolbar").forEach((item) => { item.hidden = true; });
      status("Enable authentication in Security to use accounts, locks and room schedules."); return;
    }
    return page === "users" ? users() : policy();
  }).catch((error) => status(error.message, true));
})(globalThis);
