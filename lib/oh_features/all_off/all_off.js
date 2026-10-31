// All off (lib/oh_features/all_off): ON on TABLE.item sends every
// [Item, command] of TABLE.commands: every room's Source Off, which
// routing.js routes as any choice of Off, then every power Item of a device
// openHAB controls OFF. Each is sent whatever it reads: a room or a device
// may be on its way to another state that is not read back yet.
const TABLE = null;

const openhab = require('openhab');

openhab.rules.JSRule({
  id: 'all_off',
  name: 'All off: every room and every device openHAB controls',
  triggers: [openhab.triggers.ItemCommandTrigger(TABLE.item)],
  execute: (event) => {
    if (String(event.receivedCommand) !== 'ON') return;
    const missing = [];
    for (const [name, command] of TABLE.commands) {
      const item = openhab.items.getItem(name, true);
      if (item === null) missing.push(name);
      else item.sendCommand(command);
    }
    console.info(`All off: ${TABLE.commands.length - missing.length} Items`);
    if (missing.length) console.warn(`All off: no Item ${missing.join(', ')}`);
  },
});
