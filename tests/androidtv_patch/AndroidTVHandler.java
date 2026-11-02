package org.openhab.binding.androidtv.internal;

import java.util.concurrent.ScheduledExecutorService;
import org.openhab.core.thing.Thing;
import org.openhab.core.thing.ThingUID;
import org.openhab.core.thing.ChannelUID;
import org.openhab.core.types.Command;
import org.openhab.core.types.State;

/** Test-only boundary. No binding framework, sockets, discovery or keystore. */
public class AndroidTVHandler {
    public ScheduledExecutorService scheduler;
    public String getThingID() { return "synthetic"; }
    public ThingUID getThingUID() { return new ThingUID("androidtv:googletv:synthetic"); }
    public Thing getThing() { throw new AssertionError("test must not initialize runtime/keystore"); }
    public ScheduledExecutorService getScheduler() { return scheduler; }
    public AndroidTVTranslationProvider getTranslationProvider() { return new AndroidTVTranslationProvider(); }
    public void checkThingStatus() { }
    public void setThingProperty(String key, String value) { }
    public void updateChannelState(String channel, State value) { }
    public void sendCommandToProtocol(ChannelUID channel, Command value) {
        throw new AssertionError("no commands allowed in lifecycle tests");
    }
}
