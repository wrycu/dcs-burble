local DbOption = require("Options.DbOption")

return {
  sendToAll = DbOption.new():setValue(false):checkbox(),
  hub1Url = DbOption.new():setValue(""):editbox(),
  hub1Token = DbOption.new():setValue(""):editbox(),
  hub2Url = DbOption.new():setValue(""):editbox(),
  hub2Token = DbOption.new():setValue(""):editbox(),
  hub3Url = DbOption.new():setValue(""):editbox(),
  hub3Token = DbOption.new():setValue(""):editbox(),
}
