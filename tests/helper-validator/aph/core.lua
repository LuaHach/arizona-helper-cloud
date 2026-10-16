-- Pure Lua 5.1 logic. No game APIs, network, or UI dependencies.
local speech=require 'aph.speech'
local M = {}
M.VERSION = '0.2.0'
M.MACROS = {'nick','id','my_nick','rank','org','time','server'}
local allowed = {}; for _, k in ipairs(M.MACROS) do allowed[k] = true end
function M.copy(x)
    if type(x) ~= 'table' then return x end
    local out = {}; for k,v in pairs(x) do out[k] = M.copy(v) end; return out
end
function M.trim(s) return (tostring(s or ''):gsub('^%s+',''):gsub('%s+$','')) end
function M.validateText(s)
    if type(s) ~= 'string' or M.trim(s) == '' then return nil, 'Пустая строка сообщения.' end
    if #s > 2047 then return nil, 'Строка слишком длинная.' end
    if s:find('[\r\n%z]') then return nil, 'Каждое сообщение должно быть отдельной строкой.' end
    local problem
    s:gsub('{(.-)}', function(k) if not allowed[k] then problem = 'Неизвестный макрос: {'..k..'}' end end)
    local remainder = s:gsub('{.-}', '')
    if remainder:find('[{}]') then problem = 'Незакрытая фигурная скобка.' end
    if problem then return nil,problem end
    return true
end
function M.isArray(t,limit)
    if type(t)~='table' then return false end
    local n=0
    for k in pairs(t) do
        if type(k)~='number' or k%1~=0 or k<1 then return false end
        n=n+1;if n>limit then return false end
    end
    return n==#t
end
function M.validateBind(b)
    if type(b) ~= 'table' or type(b.alias) ~= 'string' or not b.alias:match('^[%w_]+$') or #b.alias > 32 then
        return nil, 'Команда: латинские буквы, цифры и подчёркивание, без /.'
    end
    if b.alias:lower() == 'aph' or b.alias:lower() == 'aphstop' or b.alias:lower() == 'aphaccent' then return nil,'Команда зарезервирована Helper.' end
    if not M.isArray(b.rows,50) or #b.rows < 1 then return nil,'Нужно от 1 до 50 строк.' end
    for i,row in ipairs(b.rows) do
        if type(row)~='table' then return nil,'Некорректная строка сообщения.' end
        local ok,err = M.validateText(row.text)
        if not ok then return nil, 'Строка '..i..': '..err end
        if type(row.delay) ~= 'number' or row.delay ~= row.delay or row.delay < 0 or row.delay > 60000 or row.delay%1 ~= 0 then
            return nil, 'Строка '..i..': задержка — целое число 0–60000 мс.'
        end
        if row.awaitAck and row.awaitAck ~= 'gotome' then return nil,'Неизвестный тип подтверждения.' end
    end
    if b.description~=nil and (type(b.description)~='string' or #b.description>255) then return nil,'Некорректное описание.' end
    if b.enabled~=nil and type(b.enabled)~='boolean' then return nil,'Некорректный переключатель команды.' end
    if b.hotkey then
        local k = b.hotkey
        if type(k)~='table' then return nil,'Некорректная клавиша.' end
        if type(k.vk) ~= 'number' or k.vk%1 ~= 0 or k.vk<8 or k.vk>255 or k.vk==16 or k.vk==17 or k.vk==18 or (k.vk>=160 and k.vk<=165) then return nil,'Некорректная клавиша.' end
    end
    return true
end
function M.hotkeySignature(k)
    if not k then return '' end
    return table.concat({k.ctrl and '1' or '0', k.alt and '1' or '0', k.shift and '1' or '0',tostring(k.vk)},':')
end
function M.validateCollection(binds)
    if not M.isArray(binds,500) then return nil,'Некорректный список команд.' end
    local aliases,keys = {},{}
    for _,b in ipairs(binds) do
        local ok,err=M.validateBind(b); if not ok then return nil,err end
        local a=b.alias:lower(); if aliases[a] then return nil,'Повтор команды: /'..a end; aliases[a]=true
        local h=M.hotkeySignature(b.hotkey)
        if h~='' and keys[h] then return nil,'Горячая клавиша уже назначена.' end
        if b.hotkey and b.hotkey.vk==121 then return nil,'F10 зарезервирована для меню.' end
        if h~='' then keys[h]=true end
    end
    return true
end
function M.expand(text,context)
    local ok,err=M.validateText(text); if not ok then return nil,err end
    local problem
    local out=text:gsub('{(.-)}',function(k)
        local value=context[k]
        if value==nil or value=='' then problem='Нет данных для {'..k..'}'; return '' end
        value=tostring(value)
        if value:find('[\r\n%z]') then problem='Недопустимое значение {'..k..'}'; return '' end
        return value
    end)
    if problem then return nil,problem end
    return out
end
function M.prepare(bind,context,accent,ackAvailable)
    local ok,err=M.validateBind(bind); if not ok then return nil,err end
    local rows={}
    for i,row in ipairs(bind.rows) do
        if row.awaitAck and not ackAvailable then return nil,'Связка заблокирована: нет проверенного адаптера ответа /gotome.' end
        local s,e=M.expand(row.text,context); if not s then return nil,e end
        local spoken,e2=speech.apply(s,accent or '');if not spoken then return nil,e2 end;s=spoken
        rows[i]={text=s,delay=row.delay,awaitAck=row.awaitAck}
    end
    return rows
end
local Runner={}; Runner.__index=Runner
function M.runner() return setmetatable({state='idle',index=0,log={}},Runner) end
function Runner:isActive() return self.state=='waiting' or self.state=='ack' end
function Runner:start(rows,now,options)
    if self:isActive() then return nil,'Уже выполняется последовательность.' end
    if type(rows)~='table' or #rows==0 then return nil,'Нет сообщений.' end
    self.rows=M.copy(rows); self.index=1; self.state='waiting'; self.nextAt=now; self.log={}; self.reason=nil
    self.options=options or {}; self.ackTimeout=self.options.ackTimeout or 5000
    self.minInterval=self.options.minInterval or 0
    return true
end
function Runner:stop(reason,isError)
    self.state=isError and 'error' or 'stopped'; self.reason=reason or 'Остановлено'; self.rows=nil
end
function Runner:tick(now,send)
    if self.state=='ack' then
        if now>=self.ackDeadline then self:stop('Нет подтверждения /gotome: /cuff не отправлен.',true) end
        return
    end
    if self.state~='waiting' or now<self.nextAt then return end
    local row=self.rows[self.index]
    if not row then self.state='done';self.rows=nil;return end
    local ok,err=pcall(send,row.text)
    if not ok then self:stop('Ошибка отправки: '..tostring(err),true);return end
    table.insert(self.log,{at=now,text=row.text}); self.index=self.index+1
    if row.awaitAck then
        self.state='ack';self.ackKey=row.awaitAck;self.ackDeadline=now+self.ackTimeout
        self.afterAck=math.max(row.delay,self.minInterval)
    elseif self.index>#self.rows then self.state='done';self.rows=nil
    else self.nextAt=now+math.max(row.delay,self.minInterval) end
end
function Runner:ack(key,success,now)
    if self.state~='ack' or key~=self.ackKey then return false end
    if not success then self:stop('Сервер отклонил действие. Последовательность остановлена.',true)
    else self.state='waiting';self.nextAt=now+self.afterAck end
    return true
end
return M
