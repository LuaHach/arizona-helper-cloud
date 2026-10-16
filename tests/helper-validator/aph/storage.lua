local core=require 'aph.core'
local M={}
function M.migrate(data)
 if type(data)=='table' and type(data.settings)=='table' then
  if data.settings.rulesUrlTemplate==nil then data.settings.rulesUrlTemplate='' end
  data.settings.previewRole='police'
 end
 return data
end
function M.read(path)
    local f=io.open(path,'rb'); if not f then return nil,'Файл не найден.' end
    local size=f:seek('end');f:seek('set',0)
    if not size or size>4*1024*1024 then f:close();return nil,'Файл превышает 4 МБ или не читается.' end
    local s=f:read('*a'); f:close()
    if not s then return nil,'Ошибка чтения файла.' end
    local ok,data=pcall(decodeJson,s)
    if not ok or type(data)~='table' then return nil,'Некорректный JSON.' end
    return data
end
function M.validateConfig(data)
    if type(data)~='table' or data.formatVersion~=1 or type(data.binds)~='table' or type(data.settings)~='table' then return nil,'Неизвестный формат настроек.' end
    for _,role in ipairs({'police','fbi','military'}) do
        local ok,err=core.validateCollection(data.binds[role]);if not ok then return nil,role..': '..err end
    end
    local s=data.settings
    if type(s.scale)~='number' or s.scale<0.75 or s.scale>1.5 or type(s.opacity)~='number' or s.opacity<0.4 or s.opacity>1 then return nil,'Некорректные настройки интерфейса.' end
    if s.previewRole~='police' and s.previewRole~='fbi' and s.previewRole~='military' then return nil,'Неизвестная тема.' end
    if s.rulesUrlTemplate~=nil and (type(s.rulesUrlTemplate)~='string' or #s.rulesUrlTemplate>2048 or s.rulesUrlTemplate:find('[\r\n%z]')) then return nil,'Некорректная ссылка правил.' end
    if type(s.accent)~='string' or #s.accent>80 or s.accent:find('[\r\n%z]') then return nil,'Некорректный акцент.' end
    if type(s.autosave)~='boolean' or type(s.notifications)~='boolean' then return nil,'Некорректные переключатели.' end
    return true
end
function M.write(path,data)
    local ok,s=pcall(encodeJson,data);if not ok or type(s)~='string' then return nil,'Ошибка кодирования JSON.' end
    local tmp=path..'.tmp';local f,e=io.open(tmp,'wb');if not f then return nil,tostring(e) end
    local saved,err=f:write(s);f:close();if not saved then return nil,tostring(err) end
    os.remove(path..'.bak')
    local exists=io.open(path,'rb');if exists then exists:close();local moved=os.rename(path,path..'.bak');if not moved then os.remove(tmp);return nil,'Не удалось создать резервную копию.' end end
    local moved,msg=os.rename(tmp,path)
    if not moved then os.rename(path..'.bak',path);return nil,tostring(msg) end
    return true
end
function M.validateRules(data,serverKey)
    if type(data)~='table' or data.formatVersion~=1 or data.serverKey~=serverKey then return nil,'Правила относятся к другому серверу или имеют неизвестный формат.' end
    if type(data.fines)~='table' or type(data.wanted)~='table' then return nil,'Нет списков fines / wanted.' end
    for _,kind in ipairs({'fines','wanted'}) do
        if not core.isArray(data[kind],1000) then return nil,'Слишком большой список правил.' end
        for _,x in ipairs(data[kind]) do
            if type(x)~='table' or type(x.title)~='string' or type(x.text)~='string' or #x.title>512 or #x.text>20000 then return nil,'Неправильная строка правил.' end
        end
    end
    return true
end
return M
