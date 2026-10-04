declare_plugin("DCS-LSO", {
	installed = true,
	dirName = current_mod_path,
	developerName = _("dcs-lso"),
	developerLink = _("https://github.com/wrycu/dcs-lso"),
	displayName = _("DCS LSO"),
	version = "1",
	state = "installed",
	info = _("dcs-lso pilot hook: sends your carrier approaches to your communities' LSO hubs. Settings: Options > Special > DCS-LSO."),
	Options = {
		{ name = "DCS-LSO", nameId = "DCS-LSO", dir = "Options", allow_in_simulation = true; },
	},
})

plugin_done()
