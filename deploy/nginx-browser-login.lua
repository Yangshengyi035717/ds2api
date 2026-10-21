-- Verify browser_login_view = "<exp>.<hmac_sha1_hex(secret, exp)>".
-- Loaded with access_by_lua_file for both the static and WebSocket locations.
local token = ngx.var.cookie_browser_login_view or ""
local exp, mac = token:match("^(%d+)%.([0-9a-fA-F]+)$")
if not exp or not mac then
    return ngx.exit(401)
end
if tonumber(exp) <= ngx.time() then
    return ngx.exit(401)
end

local path = "/www/server/nginx/conf/browser-login.secret"
local f = io.open(path, "r")
if not f then
    return ngx.exit(503)
end
local secret = f:read("*l") or ""
f:close()
if secret == "" then
    return ngx.exit(503)
end

local raw = ngx.hmac_sha1(secret, exp)
-- ngx.hmac_sha1 returns the raw 20-byte digest, not a hex string.
local want = raw:gsub(".", function(c)
    return string.format("%02x", string.byte(c))
end)
if want ~= mac:lower() then
    return ngx.exit(401)
end
