/* Dense account/policy views and local admin forms. No runtime Item commands. */
"use strict";
const { fit, wrap } = require("./grid");
const clean = (value) => String(value == null ? "" : value).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "").slice(0, 240);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function create(client, house, changed) {
  const state = { kind: "account", room: null, loading: false, error: "", users: [], roles: [], policy: null, form: null };
  let generation = 0, ruleSerial = 0;
  function ruleId(prefix) { const ids = [...state.form.data.daily_off, ...state.form.data.locks].map((rule) => rule.id); let id; do { id = prefix + "-" + (++ruleSerial); } while (ids.includes(id)); return id; }
  const canEdit = () => client && client.canEdit();
  async function open(kind, room) {
    state.kind = kind; state.room = room || state.room || house.rooms[0]?.id; state.form = null;
    state.loading = true; state.error = ""; const turn = ++generation; changed();
    try {
      if (!client || client.native) return;
      await client.ready();
      if (kind === "settings") { const value = await client.loadPolicy(state.room); if (turn === generation) state.policy = value; }
      else if (kind === "users" && canEdit()) {
        const [users, roles] = await Promise.all([client.json("/api/v1/users"), client.json("/api/v1/roles")]);
        if (turn === generation) { state.users = users.users; state.roles = roles.roles; }
      }
    } catch (error) { if (turn === generation) state.error = error.message; }
    finally { if (turn === generation) { state.loading = false; changed(); } }
  }
  const roleLabel = (id) => state.roles.find((role) => role.id === id)?.name || "No role";
  function content(g, width, ctx) {
    let y = 0;
    const line = (text, style = "dim") => wrap(clean(text), Math.max(4, width - 2)).forEach((part) => g.put(1, y++, part, style));
    const item = (label, action, extra, disabled) => {
      const key = "access:" + action + ":" + (extra == null ? "" : extra), hit = { kind: "access-action", key, focus: true, action, id: extra, label };
      const style = disabled ? "dim" : ctx.ui.focusVisible && ctx.ui.focus === key ? "row.focus" : "link";
      if (style === "row.focus") g.fill(0, y, width, 1, style);
      g.put(1, y, fit(label, width - 2).trimEnd(), style);
      if (!disabled) g.hit(0, y, width, 1, hit); y++;
    };
    if (state.error) { line(state.error, "alert"); y++; }
    if (state.loading) { line("Reading access settings…"); return y; }
    if (!client || client.native) { line("Accounts and room schedules are paused in native testing mode."); return y; }
    if (state.kind === "account") {
      const user = client.current(); line(user ? "Signed in as " + user.username : "Not signed in", "strong");
      if (!user) { line("Run thermite access login for this address, then reopen the remote."); return y; }
      line(user?.can_edit ? "Administrator" : client.has("room.control") ? "Control of granted, unlocked rooms" : "Read-only access");
      line("Your terminal sign-in is stored in an owner-only credential file."); y++;
      item("Room settings · F7", "settings"); if (canEdit()) item("Users & roles · F8", "users");
      item("Sign out this terminal", "logout"); return y;
    }
    if (state.kind === "settings") {
      const room = house.rooms.find((room) => room.id === state.room), saved = state.policy, policy = saved?.config;
      line(room?.label || "Room", "strong");
      line(saved?.effective ? saved.effective.locked ? "Locked by schedule" : "Unlocked" : "Current policy not reported");
      line("Timezone: " + (policy?.timezone || "Site timezone"));
      line("Last verification: " + (({ verified: "Verified", degraded: "Needs attention", unknown: "Unknown" })[saved?.enforcement?.state] || "Not reported"));
      const stamp = saved?.enforcement?.updated_at;
      line("Verification time: " + (typeof stamp === "string" && Number.isFinite(Date.parse(stamp)) ? new Date(stamp).toLocaleString() : "Not reported"));
      y++; if (!canEdit()) line("Read-only schedule values", "dim");
      line(policy?.enabled ? "Schedule enabled" : "Schedule disabled", "strong");
      (policy?.daily_off || []).forEach((rule) => line("Off " + rule.time + " · " + rule.days.map((day) => DAYS[day]).join(" ")));
      (policy?.locks || []).forEach((rule) => {
        line(rule.start + "–" + rule.end + " · " + rule.days.map((day) => DAYS[day]).join(" "));
        line("  Hold " + sourceLabel(rule.desired) + " · unlock: " + (rule.unlock === "leave" ? "leave as is" : sourceLabel(rule.unlock)));
      });
      y++; item("Edit schedule", "edit-policy", null, !canEdit()); item("Reload schedule", "settings");
      y++; line("Choose a room", "strong"); house.rooms.forEach((room) => item(room.label, "room", room.id)); return y;
    }
    if (!canEdit()) { line("Only administrators can manage users and roles."); return y; }
    g.put(1, y++, fit("Username", Math.min(24, Math.floor(width / 3))) + "  " + fit("Role", 13) + " Status", "strong");
    state.users.forEach((user) => item(fit(clean(user.username), Math.min(24, Math.floor(width / 3))) + "  " + fit(roleLabel(user.role), 13) + " " + (user.active ? "Active" : "Disabled"), "edit-user", user.id));
    item("+ Add user", "new-user"); y++; line("Roles", "strong");
    state.roles.forEach((role) => {
      item(role.name + (role.reserved ? " · built-in" : ""), "edit-role", role.id, role.reserved);
      line(role.permissions.map((id) => ({ "site.read": "View site", "room.control": "Control rooms", "settings.manage": "Settings", "users.manage": "Users", "roles.manage": "Roles", "pairing.manage": "Pairing" })[id] || "").filter(Boolean).join(" · "));
    }); item("+ Add role", "new-role"); return y;
  }
  function sourceLabel(id) { return id === "off" ? "Off" : house.sources[id]?.label || id; }
  const field = (key, label, object, property, type = "text", options) => ({ key, label, type, options, get: () => object[property], set: (value) => { object[property] = value; } });
  function fields() {
    const form = state.form; if (!form) return [];
    const data = form.data, rows = [];
    if (form.kind === "user") {
      rows.push(field("username", "Username", data, "username"), field("password", form.id ? "New password (blank keeps it)" : "Password", data, "password", "password"),
        field("role", "Role", data, "role", "choice", state.roles.map((role) => [role.id, role.name])), field("active", "Active", data, "active", "boolean"), field("all", "All rooms", data, "all", "boolean"));
      if (!data.all) house.rooms.forEach((room) => rows.push({ key: "room:" + room.id, label: room.label, type: "boolean", get: () => data.rooms.includes(room.id), set: (value) => { data.rooms = data.rooms.filter((id) => id !== room.id); if (value) data.rooms.push(room.id); } }));
    } else if (form.kind === "role") rows.push(field("name", "Role name", data, "name"), ...["site.read", "room.control"].map((id) => ({ key: id, label: id === "site.read" ? "View site" : "Control granted rooms", type: "boolean", get: () => data.permissions.includes(id), set: (value) => { data.permissions = data.permissions.filter((entry) => entry !== id); if (value) data.permissions.push(id); } })));
    else {
      rows.push(field("enabled", "Schedule enabled", data, "enabled", "boolean"));
      const room = house.rooms.find((room) => room.id === state.room), sources = [["off", "Off"], ...room.sources.map((id) => [id, sourceLabel(id)])];
      for (const kind of ["daily_off", "locks"]) data[kind].forEach((rule, index) => {
        for (const key of kind === "daily_off" ? ["time"] : ["start", "end", "desired", "unlock"]) rows.push(field(kind + ":" + index + ":" + key,
          (kind === "daily_off" ? "Off " : "Lock ") + (index + 1) + " · " + ({ time: "time", start: "start", end: "end", desired: "hold", unlock: "after unlock" })[key], rule, key,
          ["desired", "unlock"].includes(key) ? "choice" : "time", key === "unlock" ? [["leave", "Leave as is"], ...sources] : sources));
        DAYS.forEach((day, dayId) => rows.push({ key: kind + ":" + index + ":day:" + dayId, label: "  " + day, type: "boolean", get: () => rule.days.includes(dayId), set: (value) => { rule.days = rule.days.filter((id) => id !== dayId); if (value) rule.days.push(dayId); } }));
        rows.push({ key: "remove:" + kind + ":" + index, label: "Remove " + (kind === "daily_off" ? "daily off " : "lock ") + (index + 1), type: "remove", get: () => "", set: () => data[kind].splice(index, 1) });
      });
    }
    return rows;
  }
  function formContent(g, width, ctx) {
    let y = 0; const form = state.form; if (!form) return y;
    const line = (text, style) => wrap(clean(text), width - 2).forEach((part) => g.put(1, y++, part, style || "dim"));
    line(form.kind === "policy" ? "Times use the site timezone. ↑↓ fields, ←→ choices; type to edit." : "↑↓ fields, ←→ choices; type to edit.");
    fields().forEach((entry) => {
      const key = "access-field:" + entry.key, active = ctx.ui.focus === key && ctx.ui.focusVisible;
      let value = entry.get();
      if (entry.type === "password") value = "•".repeat(Math.min(24, value.length));
      else if (entry.type === "boolean") value = value ? "[x]" : "[ ]";
      else if (entry.type === "choice") value = entry.options.find((choice) => choice[0] === value)?.[1] || "Choose…";
      line(entry.label + (entry.type === "remove" ? "" : ": " + value), active ? "row.focus" : "base");
      g.hit(0, y - 1, width, 1, { kind: "access-field", key, field: entry.key, focus: true, label: entry.label });
    });
    if (state.error) line(state.error, "alert");
    const action = (label, id) => { const key = "access:" + id; g.put(1, y++, label, ctx.ui.focus === key ? "row.focus" : "link", { kind: "access-action", action: id, key, focus: true, label }); };
    if (form.kind === "role" && form.id) action("Delete role", "delete-role");
    if (form.kind === "policy") { action("+ Add daily off", "add-off"); action("+ Add lock window", "add-lock"); }
    action(state.loading ? "Saving…" : "Save", "save"); action("Cancel", "cancel"); return y;
  }
  function edit(kind, existing) {
    if (!canEdit()) return;
    state.error = "";
    const data = kind === "user" ? { username: "", password: "", role: state.roles.find((role) => role.name === "User")?.id, active: true, rooms: house.rooms.map((room) => room.id), all: true, ...existing, password: "" }
      : kind === "role" ? { name: "", permissions: ["site.read"], ...existing }
      : { version: 1, enabled: false, daily_off: [], locks: [], ...state.policy?.config };
    if (kind === "user" && existing) { data.all = existing.rooms === null; data.rooms = existing.rooms || house.rooms.map((room) => room.id); }
    state.form = { kind, id: existing?.id, data: JSON.parse(JSON.stringify(data)) }; changed();
  }
  async function save() {
    if (!state.form || state.loading || !canEdit()) return;
    const form = state.form, value = JSON.parse(JSON.stringify(form.data)), turn = generation;
    delete value.id; delete value.reserved;
    if (form.kind === "user") { value.rooms = value.all ? null : value.rooms; delete value.all; if (!value.password) delete value.password; form.data.password = ""; }
    state.loading = true; changed();
    try {
      if (form.kind === "policy") await client.savePolicy(state.room, state.policy.revision, value);
      else await client.json("/api/v1/" + (form.kind === "user" ? "users" : "roles") + (form.id ? "/" + form.id : ""), form.id ? "PATCH" : "POST", value);
      if (turn !== generation) return;
      state.form = null; await open(state.kind, state.room);
    } catch (error) { if (turn === generation) state.error = error.message; }
    finally { if (turn === generation) { state.loading = false; changed(); } }
  }
  function activate(hit) {
    if (hit.kind === "access-field") {
      const entry = fields().find((entry) => entry.key === hit.field); if (!entry || !canEdit()) return;
      if (entry.type === "boolean") entry.set(!entry.get());
      else if (entry.type === "choice") entry.set(entry.options[(entry.options.findIndex((choice) => choice[0] === entry.get()) + 1) % entry.options.length][0]);
      else if (entry.type === "remove") entry.set(); changed(); return;
    }
    switch (hit.action) {
      case "room": return open("settings", hit.id);
      case "settings": return open("settings", state.room);
      case "users": return open("users");
      case "edit-user": return edit("user", state.users.find((user) => user.id === hit.id));
      case "new-user": return edit("user");
      case "edit-role": { const role = state.roles.find((role) => role.id === hit.id); if (role && !role.reserved) return edit("role", role); break; }
      case "new-role": return edit("role");
      case "edit-policy": return edit("policy");
      case "delete-role": {
        if (!canEdit() || !state.form || state.form.kind !== "role" || !state.form.id || state.loading) return;
        const form = state.form, turn = generation; state.loading = true; changed();
        return client.json("/api/v1/roles/" + form.id, "DELETE").then(() => {
          if (turn === generation) { state.form = null; return open("users"); }
        }).catch((error) => { if (turn === generation) state.error = error.message; })
          .finally(() => { if (turn === generation) { state.loading = false; changed(); } });
      }
      case "save": return save();
      case "cancel": state.form = null; changed(); break;
      case "add-off": state.form.data.daily_off.push({ id: ruleId("daily"), time: "22:00", days: [0, 1, 2, 3, 4, 5, 6] }); changed(); break;
      case "add-lock": state.form.data.locks.push({ id: ruleId("lock"), start: "23:00", end: "07:00", days: [0, 1, 2, 3, 4, 5, 6], desired: "off", unlock: "leave" }); changed(); break;
      case "logout": client.logout().catch(() => {}).finally(changed); break;
    }
  }
  function key(ch, event, hit) {
    if (!state.form || !hit || hit.kind !== "access-field") return false;
    const entry = fields().find((field) => field.key === hit.field); if (!entry) return false;
    if (["left", "right"].includes(event.name) && entry.type === "choice") {
      const index = entry.options.findIndex((choice) => choice[0] === entry.get()); entry.set(entry.options[(index + (event.name === "left" ? -1 : 1) + entry.options.length) % entry.options.length][0]); changed(); return true;
    }
    if (["text", "time", "password"].includes(entry.type)) {
      if (event.name === "backspace") { entry.set(entry.get().slice(0, -1)); changed(); return true; }
      if (typeof ch === "string" && ch.length && !event.ctrl && !event.meta && !/[\x00-\x1f\x7f-\x9f]/.test(ch)) {
        if (entry.type !== "time" || /^[0-9:]+$/.test(ch)) entry.set((entry.get() + ch).slice(0, entry.type === "password" ? 1024 : entry.type === "time" ? 5 : 150)); changed(); return true;
      }
    }
    return false;
  }
  return { native: !client || client.native, state, open, content, formContent, activate, key, cancel: () => { generation++; state.loading = false; if (state.form?.data) state.form.data.password = ""; state.form = null; }, canEdit };
}
module.exports = { create };
