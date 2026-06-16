 Задача ​

Получение информации о задаче DL
Задача Operations

    get/get-task-info
    post/get-solution

Получение информации о задаче по nodeId​

Возвращает название задачи, её ID и формулировку по переданному nodeId.
Query Parameters

    nodeId

Type: integer
required

Id ноды, для которой нужно получить информацию о задаче
removeHtmlTags

    Type: boolean
    default:  
    true

    Удалять ли HTML-теги из текстов (по умолчанию true)

Responses

    Type: object
        name
        Type: string

        Название задачи
        statement
        Type: string

        Формулировка задачи
        taskId
        Type: integer

        Id задачи
    application/json
    404

    Задача для переданного nodeId не найдена

Request Example for get/get-task-info

import http.client

conn = http.client.HTTPSConnection("dl.gsu.by")

conn.request("GET", "/restapi/get-task-info")

res = conn.getresponse()
data = res.read()

print(data.decode("utf-8"))

{
  "name": "string",
  "taskId": 1,
  "statement": "string"
}

Успешный ответ с информацией о задаче
Получение примера решения задачи​

Позволяет получить содержимое файла решения для заданной задачи, если у пользователя есть права на использование AI и файл решения существует.
Body
required
application/json

    fileExtension

Type: string
required

Расширение файла решения (например .pas, .cpp, .py, .java, можно любое), ищется в папке задачи по маске *sol* c выбранным расширением
sessionId
Type: string
required

Session Id хранится в Url-encoded виде внутри cookies. Он существует и валиден некоторое время после того как человек заходит в аккаунт DL из браузера.

Выглядит он примерно так: {2DA21836-FD30-433F-B0A3-A4BDA2669B6D}

Для получения вручную нужно:

    Зайти в devtools браузера

    Перейти на вкладку Application

    Раздел Cookies -> https://dl.gsu.by

    Найти DLSID

    Включить снизу галочку "Show URL-decoded"

    Скопировать значение

Для получения через JavaScript можно использовать следующий код:

function getSessionId() {
    const match = document.cookie.match('(?:^|; )DLSID=([^;]*)');
    return match ? decodeURIComponent(match[1]) : null;
}

console.log(getSessionId());

taskId

    Type: integer
    required

    Id задачи, для которой запрашивается решение

Responses

    Type: object
        solution
        Type: string

        Содержимое файла решения
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    403

    Доступ запрещён — пользователь не может использовать AI для этой задачи
    404

    Файл решения не найден
    500

    Внутренняя ошибка сервера

Request Example for post/get-solution

import http.client

conn = http.client.HTTPSConnection("dl.gsu.by")

payload = "{\"sessionId\":\"\",\"fileExtension\":\"\",\"taskId\":1}"

headers = { 'Content-Type': "application/json" }

conn.request("POST", "/restapi/get-solution", payload, headers)

res = conn.getresponse()
data = res.read()

print(data.decode("utf-8"))

{
  "solution": "string"
}

Успешный ответ с содержимым файла решения


 Models

    Type: string

    Session Id хранится в Url-encoded виде внутри cookies. Он существует и валиден некоторое время после того как человек заходит в аккаунт DL из браузера.

    Выглядит он примерно так: {2DA21836-FD30-433F-B0A3-A4BDA2669B6D}

    Для получения вручную нужно:

        Зайти в devtools браузера

        Перейти на вкладку Application

        Раздел Cookies -> https://dl.gsu.by

        Найти DLSID

        Включить снизу галочку "Show URL-decoded"

        Скопировать значение

    Для получения через JavaScript можно использовать следующий код:

    function getSessionId() {
        const match = document.cookie.match('(?:^|; )DLSID=([^;]*)');
        return match ? decodeURIComponent(match[1]) : null;
    }

    console.log(getSessionId());

    sessionId
    Type: string
    required

    Session Id хранится в Url-encoded виде внутри cookies. Он существует и валиден некоторое время после того как человек заходит в аккаунт DL из браузера.

    Выглядит он примерно так: {2DA21836-FD30-433F-B0A3-A4BDA2669B6D}

    Для получения вручную нужно:

        Зайти в devtools браузера

        Перейти на вкладку Application

        Раздел Cookies -> https://dl.gsu.by

        Найти DLSID

        Включить снизу галочку "Show URL-decoded"

        Скопировать значение

    Для получения через JavaScript можно использовать следующий код:

    function getSessionId() {
        const match = document.cookie.match('(?:^|; )DLSID=([^;]*)');
        return match ? decodeURIComponent(match[1]) : null;
    }

    console.log(getSessionId());

    canUseAi
    Type: boolean

    Флаг — разрешено ли использование AI в текущем курсе
    courseID
    Type: integer

    Id курса, в которой сейчас находится пользователь
    currentStatement
    Type: string

    Текст задачи, в которой сейчас находится пользователь
    nodeId
    Type: integer

    Id ноды, в которой сейчас находится пользователь
    taskId
    Type: integer

    Id задачи, в которой сейчас находится пользователь
    userId
    Type: integer

    Id пользователя

    name
    Type: string

    Название задачи
    statement
    Type: string

    Формулировка задачи
    taskId
    Type: integer

    Id задачи

    fileExtension
    Type: string
    required

    Расширение файла решения (например .pas, .cpp, .py, .java, можно любое), ищется в папке задачи по маске *sol* c выбранным расширением
    sessionId
    Type: string
    required

    Session Id хранится в Url-encoded виде внутри cookies. Он существует и валиден некоторое время после того как человек заходит в аккаунт DL из браузера.

    Выглядит он примерно так: {2DA21836-FD30-433F-B0A3-A4BDA2669B6D}

    Для получения вручную нужно:

        Зайти в devtools браузера

        Перейти на вкладку Application

        Раздел Cookies -> https://dl.gsu.by

        Найти DLSID

        Включить снизу галочку "Show URL-decoded"

        Скопировать значение

    Для получения через JavaScript можно использовать следующий код:

    function getSessionId() {
        const match = document.cookie.match('(?:^|; )DLSID=([^;]*)');
        return match ? decodeURIComponent(match[1]) : null;
    }

    console.log(getSessionId());

    taskId
    Type: integer
    required

    Id задачи, для которой запрашивается решение

    solution
    Type: string

    Содержимое файла решения
