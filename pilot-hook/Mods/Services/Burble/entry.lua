declare_plugin("Burble", {
	installed = true,
	dirName = current_mod_path,
	developerName = _("Burble"),
	developerLink = _("https://github.com/wrycu/dcs-burble"),
	displayName = _("Burble"),
	version = "1",
	state = "installed",
	info = _("Burble pilot hook: sends your carrier approaches to your communities' LSO hubs. Settings: Options > Special > Burble."),
	Options = {
		{ name = "Burble", nameId = "Burble", dir = "Options", allow_in_simulation = true; },
	},
})

plugin_done()
