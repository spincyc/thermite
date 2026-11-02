<section class="hero">
  <div class="hero-mark">{{torch_large}}</div>
  <div>
    <h1>thermite</h1>
    <p class="lede">Home automation you can own.</p>
    <p>Describe your rooms, devices and connections in one file. thermite builds your <a href="https://www.openhab.org/">openHAB</a> configuration and gives you everyday controls in a web app and a terminal.</p>
    <p class="cta"><a class="button" href="start.html">Get started</a> <a class="button ghost" href="#see-it">See the remotes</a></p>
  </div>
</section>

<div class="cards">
  <div class="card"><h3>One house file</h3><p>Keep rooms, devices and their connections together. thermite generates the runtime configuration.</p></div>
  <div class="card"><h3>Checked deployments</h3><p>Validate the configuration, check openHAB startup, and keep earlier deployments available for rollback.</p></div>
  <div class="card"><h3>A remote on every screen</h3><p>Choose sources and control devices from your phone, desktop browser or terminal.</p></div>
  <div class="card"><h3>Yours, on your network</h3><p>Core integrations communicate locally. Your real inventory and configuration stay in your private site.</p></div>
</div>

## See it

<figure class="screenshot">
  <a href="assets/screenshots/web-remote.png"><img src="assets/screenshots/web-remote.png" width="1440" height="1000" alt="thermite web app with rooms on the left, player remote in the center and device information on the right" loading="lazy"></a>
  <figcaption><strong>The web app.</strong> Rooms, sources, device controls and signal information, with a layout that adapts to your screen.</figcaption>
</figure>

<figure class="screenshot">
  <a href="assets/screenshots/terminal-remote.png"><img src="assets/screenshots/terminal-remote.png" width="1232" height="880" alt="thermite terminal remote showing the fictional Example West room and player controls" loading="lazy"></a>
  <figcaption><strong>The terminal remote.</strong> The same room model and controls, with keyboard and mouse navigation.</figcaption>
</figure>

Both images show demo mode with fictional inventory, captured from the actual clients.

## Run it your way

Already have a Linux server? Follow the [server installation](installation.html). Setting aside a machine for thermite? Build a bootable image with the [standalone ISO guide](iso.html). Both paths use a private site for your inventory and configuration.

## How it works

1. **Describe** the rooms, devices and connections in your house file.
2. **Generate** openHAB's configuration and the remotes.
3. **Check** the inventory and generated files, then verify that openHAB loads them.
4. **Deploy** the checked configuration to your server.
5. **Use** the remotes to choose rooms, switch sources and control devices.

## What it controls

Supported integrations cover TVs, streaming boxes, AV receivers, projectors, speakers, and HDMI and USB switchers. Each device needs a compatible control interface; the [commands guide](commands.html) includes discovery and pairing tools.

## Why the name

The name draws on aluminothermic metalworking and the craft of joining useful parts. The bowl mark brings together pewter, copper and a little heat. [Meet the bowl and its colors](colors.html).
