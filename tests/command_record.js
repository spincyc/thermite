'use strict';
// What a run of a production device script recorded of its commands
// (oh_integrations/_commands.py, _device.js recorder), for the harnesses
// that run those scripts: read from its log ([level, message]) and from what
// it posted to Items ([Item, state]).
const RECORD = /^command device=([a-z0-9_]+) item=(\S+) command=(\S+) outcome=([a-z_]+) code=([a-z_]+|-) ms=(\d+)$/;

function records(logs, posted) {
  const record = ([, message]) => message.startsWith('command device=');
  return {
    // The record lines, each as [level, Item, command, outcome, code].
    recorded: () => logs.filter(record).map(([level, message]) => {
      const [, , item, command, outcome, code] = RECORD.exec(message);
      return [level, item, command, outcome, code];
    }),
    // The counts above zero of the last tally posted to an Item, by outcome.
    tally: (name) => {
      const rows = posted.filter(([item]) => item === name);
      const counted = Object.entries(JSON.parse(rows[rows.length - 1][1]).outcomes).filter(([, row]) => row.count);
      return Object.fromEntries(counted.map(([outcome, row]) => [outcome, row.count]));
    },
    // Each state a problem Item was given, in order.
    problems: (name) => posted.filter(([item]) => item === name).map(([, state]) => state),
    // What else the script logged.
    said: () => logs.filter((row) => !record(row)),
  };
}

module.exports = { RECORD, records };
