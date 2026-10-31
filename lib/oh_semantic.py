"""openHAB's semantic tags and the device kinds of instance/devices.yaml.

The tag sets are openHAB's default semantic tags, copied from openhab-core's
bundles/org.openhab.core.semantics/model/SemanticTags.csv by its Type column
(Location, Equipment, Point, Property). Each set also holds its root tag
(`Location`, `Equipment`, ...), which openHAB accepts as a tag too.
"""

from __future__ import annotations

LOCATION_TAGS = frozenset("""
    Location
    Apartment Attic Basement Bathroom Bedroom BoilerRoom Building Carport Cellar
    Corridor DiningRoom Driveway Entry FamilyRoom FirstFloor Floor Garage Garden
    GroundFloor GuestRoom House Indoor Kitchen LaundryRoom LivingRoom Office Outdoor
    Patio Porch Room SecondFloor Shed SummerHouse Terrace ThirdFloor Veranda
""".split())

EQUIPMENT_TAGS = frozenset("""
    Equipment
    AccentLight AirConditioner AirFilter AirFryer AirQualitySensor AlarmDevice
    AlarmSystem AlarmZone Application Aquarium AudioVisual BackDoor Battery Bed Blinds
    Boiler Button CO2Sensor COSensor Camera Car CeilingFan CellarDoor Chandelier
    Chlorinator CleaningRobot CoffeeMaker Computer ContactSensor ControlDevice Cooktop
    Dehumidifier Dial Dishwasher Display Door Doorbell Downlight Drapes DrinkingWater
    Dryer EVSE ElectricMeter ExhaustFan Fan FireDetector Firewall FlameDetector
    FloodLight FloorHeating FoodProcessor Freezer FrontDoor Fryer Furnace GarageDoor
    GasMeter Gate Generator GlassBreakDetector HVAC HeatDetector HeatPump HeatRecovery
    Horticulture HotWaterFaucet Humidifier HumiditySensor IceMaker IlluminanceSensor
    InnerDoor Inverter Irrigation Jacuzzi Keypad KitchenHood Lamp LawnMower LeakSensor
    LightSource LightStrip LightStripe Lightbulb Lock MediaPlayer Microwave Mixer
    MotionDetector NetworkAppliance NetworkSwitch OccupancySensor Oven Pendant PetCare
    PetFeeder PetFlap PoolCover PoolHeater PowerOutlet PowerSupply Printer Printer3D
    Projector Pump RadiatorControl Range Receiver Refrigerator RemoteControl Router
    Sauna Sconce Screen Sensor Shower SideDoor Siren Slider SmartVent Smartphone
    SmokeDetector SoilSensor SolarPanel Speaker SpotLight SwimmingPool Television
    TemperatureSensor Thermostat Toaster Tool TrackLight Tracker TransferSwitch UPS
    Valve Vehicle VibrationSensor VoiceAssistant WallLight WallSwitch WashingMachine
    WaterFeature WaterFilter WaterHeater WaterMeter WaterQualitySensor WaterSoftener
    WeatherService WeatherStation WebService Wellness WhiteGood WindGenerator Window
    WindowCovering WirelessAccessPoint Zone
""".split())

POINT_TAGS = frozenset("""
    Point
    Alarm Calculation Control Forecast Measurement Setpoint Status Switch
""".split())

PROPERTY_TAGS = frozenset("""
    Property
    AQI AirQuality Airconditioning Airflow App Brightness CO CO2 Channel Color
    ColorTemperature Current Duration Enabled Energy Frequency Gas GeoLocation Heating
    Humidity Illuminance Info Level Light LockState LowBattery MediaControl Mode
    Moisture Motion Noise Oil OpenLevel OpenState Opening Ozone ParticulateMatter Pollen
    Position Power Precipitation Presence Pressure Price Progress QualityOfService RSSI
    Radon Rain SignalStrength Smoke SoundVolume Speed StateOfCharge Tampered Temperature
    Tilt Timestamp Ultraviolet VOC Ventilation Vibration Voltage Water Wind
""".split())

#: Every device `kind` devices.yaml may use, with the Equipment tag of the
#: device's group in the semantic model. A new kind is added here deliberately.
KINDS = {
    "tv": "Television",
    "monitor": "Screen",
    "avr": "Receiver",
    "streaming_amp": "MediaPlayer",
    "soundbar": "Speaker",
    "amp": "Receiver",
    "sub": "Speaker",
    "streamer": "MediaPlayer",
    "disc_player": "MediaPlayer",
    "game_console": "MediaPlayer",
    "hdmi_matrix": "AudioVisual",
    "hdmi_switch": "AudioVisual",
    "hdmi_processor": "AudioVisual",
    "usb_matrix": "Equipment",
    "power": "PowerSupply",
    "control4_controller": "ControlDevice",
    "control4_remote": "RemoteControl",
    "lighting": "LightSource",
    "curtain": "Drapes",
    "projector": "Projector",
    "computer": "Computer",
    "camera": "Camera",
    "doorbell": "Doorbell",
}
