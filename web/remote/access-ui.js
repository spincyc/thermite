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
    try {
      const user = await client.ready();
      if (user) {
        $(".access-login h1").textContent = "Your account"; form.replaceChildren();
        form.append(node("p", "Signed in as " + user.username), button("Sign out", () => client.logout().then(() => location.reload())));
        status(user.can_edit ? "Administrator" : client.has("room.control") ? "Room controls available" : "Read-only access"); return;
      }
    } catch (_) { /* A sign-in page deliberately starts without a session. */ }
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const username = form.elements.username.value, password = form.elements.password.value;  // lint: allow — reads the password field; no embedded credential
      form.elements.password.value = ""; const submit = form.querySelector("button"); submit.disabled = true; status("Signing in…");
      try {
        await client.login(username, password);
        location.replace(global.ThermiteAccess.returnTo(new URLSearchParams(location.search).get("next"), location.origin));
      } catch (error) { status(error.message, true); form.elements.password.focus(); }
      finally { submit.disabled = false; }
    });
    status("");
  }
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function days(values) {
    const field = node("div", null, { class: "access-days" });
    DAYS.forEach((name, id) => { const label = input(name, "day", "checkbox", values.includes(id)); label.firstElementChild.value = id; field.appendChild(label); });
    return field;
  }
  const selectedDays = (row) => Array.from(row.querySelectorAll('[name="day"]:checked'), (item) => Number(item.value));
  let turn = 0, revision = 0, currentRoom = null, serial = 0;
  async function policy() {
    const selector = $("[data-policy-room]");
    house.rooms.forEach((room) => selector.appendChild(node("option", room.label, { value: room.id })));
    let requested = new URLSearchParams(location.search).get("room");
    if (!requested) { try { requested = localStorage.getItem("remote.room"); } catch (_) {} }
    if (house.rooms.some((room) => room.id === requested)) selector.value = requested;
    selector.addEventListener("change", readPolicy); $("[data-policy-reload]").addEventListener("click", readPolicy); await readPolicy();
  }
  async function readPolicy() {
    const current = ++turn, room = $("[data-policy-room]").value; currentRoom = room; status("Reading schedule…");
    try { const answer = await client.loadPolicy(room); if (current !== turn) return; revision = answer.revision; drawPolicy(answer); status(""); }
    catch (error) { if (current === turn) { $("[data-policy]").replaceChildren(); status(error.message, true); } }
  }
  function drawPolicy(answer) {
    const room = house.rooms.find((entry) => entry.id === currentRoom), editable = answer.can_edit === true && client.canEdit();
    const config = answer.config || { version: 1, enabled: false, daily_off: [], locks: [] };
    const host = $("[data-policy]"); host.replaceChildren();
    const summary = node("div", null, { class: "access-summary" });
    const fact = (name, value) => { const box = node("span", value); box.prepend(node("strong", name)); summary.appendChild(box); };
    fact("Schedule timezone", config.timezone || "Site timezone");
    fact("Current policy", answer.effective?.locked ? "Locked" : answer.effective ? "Unlocked" : "Not reported");
    fact("Last verification", ({ verified: "Verified", degraded: "Needs attention", unknown: "Unknown" })[answer.enforcement?.state] || "Not reported");
    const stamp = answer.enforcement?.updated_at;
    fact("Verification time", typeof stamp === "string" && Number.isFinite(Date.parse(stamp)) ? new Date(stamp).toLocaleString() : "Not reported");
    if (answer.effective?.locked && answer.effective.desired) fact("Lock holds", answer.effective.desired === "off" ? "Off" : house.sources[answer.effective.desired]?.label || "Source unavailable");
    host.appendChild(summary);
    if (!editable) host.appendChild(node("p", "Schedule values are read-only for your account.", { class: "access-readonly" }));
    const form = node("form", null, { class: "access-form access-form--wide" }), fields = node("fieldset"); fields.disabled = !editable;
    const enabled = input("Enable room schedule", "enabled", "checkbox", config.enabled); enabled.className = "access-check"; fields.appendChild(enabled);
    const sources = [["off", "Off"], ...room.sources.map((id) => [id, house.sources[id]?.label || id])];
    const daily = node("div"), locks = node("div"), ruleIds = new Set([...(config.daily_off || []), ...(config.locks || [])].map((rule) => rule.id));
    function add(kind, value) {
      const row = node("div", null, { class: "access-rule" + (kind === "locks" ? " access-rule--lock" : ""), "data-rule": kind });
      if (value.id) row.dataset.id = value.id;
      else { do { row.dataset.id = "rule-" + (++serial); } while (ruleIds.has(row.dataset.id)); ruleIds.add(row.dataset.id); }
      if (kind === "daily_off") row.appendChild(input("Turn off at", "time", "time", value.time || "22:00"));
      else row.append(input("Lock starts", "start", "time", value.start || "23:00"), input("Lock ends", "end", "time", value.end || "07:00"),
        select("During lock", "desired", sources, value.desired || "off"), select("After unlock", "unlock", [["leave", "Leave as is"], ...sources], value.unlock || "leave"));
      row.append(button("Remove", () => row.remove()), days(value.days || [0, 1, 2, 3, 4, 5, 6]));
      (kind === "locks" ? locks : daily).appendChild(row);
    }
    fields.append(node("h2", "Daily off"), daily); (config.daily_off || []).forEach((value) => add("daily_off", value));
    fields.appendChild(button("Add daily off", () => add("daily_off", {})));
    fields.append(node("h2", "Lock windows"), node("p", "Locks hold the chosen source or Off. Overnight windows end the next day.", { class: "access-note" }), locks);
    (config.locks || []).forEach((value) => add("locks", value)); fields.appendChild(button("Add lock window", () => add("locks", {})));
    form.appendChild(fields); const save = node("button", "Save schedule", { type: "submit" }); save.disabled = !editable; form.appendChild(save); host.appendChild(form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (!editable) return;
      const value = { version: 1, enabled: fields.querySelector('[name="enabled"]').checked, daily_off: [], locks: [] };
      fields.querySelectorAll("[data-rule]").forEach((row) => {
        const kind = row.dataset.rule, rule = { id: row.dataset.id, days: selectedDays(row) };
        (kind === "locks" ? ["start", "end", "desired", "unlock"] : ["time"]).forEach((key) => { rule[key] = row.querySelector('[name="' + key + '"]').value; }); value[kind].push(rule);
      });
      save.disabled = true; const roomId = currentRoom, generation = turn;
      try { const saved = await client.savePolicy(roomId, revision, value); if (turn === generation) { revision = saved.revision; status("Schedule saved."); } }
      catch (error) { if (turn === generation) status(error.message, true); }
      finally { if (turn === generation) save.disabled = false; }
    });
  }
  async function users() {
    if (!client.canEdit()) { status("Only administrators can manage users and roles.", true); return; }
    let userList = [], roles = [];
    async function reload() {
      const [a, b] = await Promise.all([client.json("/api/v1/users"), client.json("/api/v1/roles")]); userList = a.users; roles = b.roles; draw(); status("");
    }
    const roleLabel = (id) => roles.find((role) => role.id === id)?.name || "No role";
    function draw() {
      const section = $("[data-users]"), roleSection = $("[data-roles]"); section.replaceChildren(); roleSection.replaceChildren();
      const bar = node("div", null, { class: "access-toolbar" }); bar.append(node("h2", "Users"), button("Add user", () => editUser())); section.appendChild(bar);
      const table = node("table", null, { class: "access-table" }), head = node("thead"), titles = node("tr"), body = node("tbody");
      ["Username", "Role", "Rooms", "Status", "Actions"].forEach((title) => titles.appendChild(node("th", title, { scope: "col" }))); head.appendChild(titles); table.append(head, body);
      userList.forEach((user) => { const row = node("tr");
        [user.username, roleLabel(user.role), user.rooms === null ? "All rooms" : user.rooms.map((id) => house.rooms.find((room) => room.id === id)?.label || id).join(", ") || "None", user.active ? "Active" : "Disabled"].forEach((value) => row.appendChild(node("td", value)));
        const actions = node("td"); actions.appendChild(button("Edit", () => editUser(user))); row.appendChild(actions); body.appendChild(row);
      }); const scroll = node("div", null, { class: "access-table-wrap" }); scroll.appendChild(table); section.appendChild(scroll);
      const roleBar = node("div", null, { class: "access-toolbar" }); roleBar.append(node("h2", "Roles"), button("Add role", () => editRole())); roleSection.appendChild(roleBar);
      const list = node("table", null, { class: "access-table", "aria-label": "Roles and permissions" }), roleBody = node("tbody"), roleHead = node("thead"), roleTitles = node("tr");
      ["Role", "Permissions", "Actions"].forEach((title) => roleTitles.appendChild(node("th", title, { scope: "col" }))); roleHead.appendChild(roleTitles); list.append(roleHead, roleBody);
      roles.forEach((role) => { const row = node("tr"); row.append(node("td", role.name), node("td", role.permissions.map((permission) => ({ "site.read": "View site", "room.control": "Control rooms", "settings.manage": "Manage settings", "users.manage": "Manage users", "roles.manage": "Manage roles", "pairing.manage": "Pair devices" })[permission] || "").filter(Boolean).join(" · ")));
        const cell = node("td"); if (role.reserved) cell.appendChild(node("span", "Built-in", { class: "access-note" })); else cell.appendChild(button("Edit", () => editRole(role))); row.appendChild(cell); roleBody.appendChild(row); });
      const roleScroll = node("div", null, { class: "access-table-wrap" }); roleScroll.appendChild(list); roleSection.appendChild(roleScroll);
    }
    function editUser(user) {
      const existing = user || { username: "", role: roles.find((role) => role.name === "User")?.id, rooms: null, active: true };
      dialog(user ? "Edit user" : "Add user", (form, modal) => {
        const username = input("Username", "username", "text", existing.username); username.firstElementChild.required = true; username.firstElementChild.maxLength = 150; username.firstElementChild.autocomplete = "off";
        const secret = input(user ? "New password (leave blank to keep)" : "Password", "password", "password"); secret.firstElementChild.autocomplete = "new-password"; secret.firstElementChild.maxLength = 1024; secret.firstElementChild.required = !user;  // lint: allow — password field labels and input type
        form.append(username, secret, select("Role", "role", roles.map((role) => [role.id, role.name]), existing.role));
        const all = input("Grant all rooms", "all", "checkbox", existing.rooms === null); all.className = "access-check";
        const grants = node("fieldset", null, { class: "access-room-grants" }); grants.appendChild(node("legend", "Room controls"));
        house.rooms.forEach((room) => { const label = input(room.label, "room", "checkbox", existing.rooms === null || existing.rooms.includes(room.id)); label.className = "access-check"; label.firstElementChild.value = room.id; grants.appendChild(label); });
        const update = () => { grants.disabled = all.firstElementChild.checked; }; all.firstElementChild.addEventListener("change", update); update();
        const active = input("Active account", "active", "checkbox", existing.active); active.className = "access-check"; form.append(all, grants, active);
        form.addEventListener("submit", async (event) => { event.preventDefault(); const value = { username: form.elements.username.value, role: Number(form.elements.role.value), active: form.elements.active.checked,
          rooms: form.elements.all.checked ? null : Array.from(grants.querySelectorAll("input:checked"), (field) => field.value) };
          if (form.elements.password.value) value.password = form.elements.password.value; form.elements.password.value = "";  // lint: allow — copies entered data, then clears the field
          const save = form.querySelector('[type="submit"]'); save.disabled = true;
          try { await client.json("/api/v1/users" + (user ? "/" + user.id : ""), user ? "PATCH" : "POST", value); modal.close(); await reload(); }
          catch (error) { formError(form, error.message); }
          finally { save.disabled = false; }
        });
      });
    }
    function editRole(role) {
      dialog(role ? "Edit role" : "Add role", (form, modal) => {
        const name = input("Role name", "name", "text", role?.name); name.firstElementChild.maxLength = 64; name.firstElementChild.required = true; form.appendChild(name);
        for (const [id, label] of [["site.read", "View site"], ["room.control", "Control granted rooms"]]) { const box = input(label, id, "checkbox", !role || role.permissions.includes(id)); box.className = "access-check"; form.appendChild(box); }
        if (role) form.appendChild(button("Delete role", async () => { try { await client.json("/api/v1/roles/" + role.id, "DELETE"); modal.close(); await reload(); } catch (error) { formError(form, error.message); } }));
        form.addEventListener("submit", async (event) => { event.preventDefault();
          try { await client.json("/api/v1/roles" + (role ? "/" + role.id : ""), role ? "PATCH" : "POST", { name: form.elements.name.value, permissions: ["site.read", "room.control"].filter((id) => form.elements.namedItem(id).checked) }); modal.close(); await reload(); }
          catch (error) { formError(form, error.message); }
        });
      });
    }
    await reload();
  }
  if (page === "access") login().catch((error) => status(error.message, true));
  else client.ready().then(() => page === "users" ? users() : policy()).catch((error) => status(error.message, true));
})(globalThis);
