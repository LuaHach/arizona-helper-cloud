-- Shared UTF-8 speech formatting for bind output and normal outgoing chat.
local M={}
local function trim(s)return (s:gsub('^%s+',''):gsub('%s+$',''))end
function M.accent(value)
 if type(value)~='string' then return nil,'Некорректный акцент.' end
 local a=trim(value)
 while a:match('^%[.*%]$') do a=trim(a:sub(2,-2)) end
 if #a>80 or a:find('[\r\n%z{}]') or a:find('[%[%]]') then return nil,'Акцент: до 80 байт, без скобок и переносов.' end
 return a
end
function M.apply(text,value)
 if type(text)~='string' or text:find('[\r\n%z]') then return nil,'Некорректное сообщение.' end
 local a,err=M.accent(value or '');if not a then return nil,err end
 local t=trim(text)
 -- Commands and OOC markers are not spoken IC text.
 if a=='' or t=='' or t:sub(1,1)=='/' or t:sub(1,2)=='((' then return text end
 local prefix='['..a..']'
 if t:sub(1,#prefix)==prefix then return text end
 return prefix..' '..text
end
function M.outgoing(wire,accent,toUtf8,toWire)
 local ok,text=pcall(toUtf8,wire);if not ok then return nil,'Не удалось прочитать сообщение.' end
 local result,err=M.apply(text,accent);if not result then return nil,err end
 local ok2,encoded=pcall(toWire,result)
 if not ok2 or type(encoded)~='string' then return nil,'Не удалось закодировать сообщение.' end
 if #encoded>127 then return nil,'Сообщение с акцентом длиннее 127 байт. Сократите текст.' end
 return encoded
end
return M
