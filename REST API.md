DL REST API

Это документация к API для DL, сделанной на принципах REST. С его помощью можно легко и интуитивно делать запросы для получение информации из-вне сервера DL. Легко адаптируются и добавляются новые endpoint'ы.

Чтобы наверняка увидеть актуальную страницу можно нажать ctrl+f5.

Ссылки:

    Страница на confluence

Server

Обычный сервер DL
Client Libraries
Python Requests
Пользователь ​

Получение информации о пользователе DL
Пользователь Operations

    post/get-user-info
    get/get-id-user-info

Получение информации о текущем пользователе​

Принимает sessionId в теле запроса и возвращает информацию о пользователе: - Если sessionId некорректен или устарел — возвращает 401 Unauthorized. - Если пользователь не находится в конкретной ноде — nodeId, taskId и currentStatement будут равны 0 (а currentStatement пустой строке).
Body
required
application/json

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

        removeHtmlTags
        Type: boolean
        default:  
        true

        Удалять ли HTML-теги из текстов (по умолчанию true)

Responses

    Type: object
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
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    500

    Внутренняя ошибка сервера

Request Example for post/get-user-info

requests.post("https://dl.gsu.by/restapi/get-user-info",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "removeHtmlTags": True
    }
)

{
  "userId": 1,
  "courseID": 1,
  "nodeId": 1,
  "taskId": 1,
  "currentStatement": "string",
  "canUseAi": true
}

Успешный ответ с информацией о пользователе
Получение базовой информации о пользователе по ID​

Возвращает имя и фамилию пользователя по его userId.
Query Parameters

    userId
    Type: integer
    required

    Id пользователя для поиска

Responses

    Type: object
        firstName
        Type: string

        Имя пользователя
        lastName
        Type: string

        Фамилия пользователя
    application/json
    404

    Пользователь с таким ID не найден
    500

    Внутренняя ошибка сервера

Request Example for get/get-id-user-info

requests.get(
    "https://dl.gsu.by/restapi/get-id-user-info",
    params={
      "userId": "1"
    }
)

{
  "firstName": "string",
  "lastName": "string"
}

Успешный ответ с информацией о пользователе
Задача ​

Получение информации о задаче DL
Задача Operations

    get/get-task-info
    post/get-solution

Получение информации о задаче по nodeId​

Возвращает название задачи, её ID, формулировку и полный путь в дереве (если передан courseId).
Query Parameters

    nodeId
    Type: integer
    required

    Id ноды, для которой нужно получить информацию о задаче
    courseId
    Type: integer
    default:  
    0

    Id курса. Необходим для получения полного пути к задаче в дереве (поле path)
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
        path
        Type: string | null

        Полный путь к задаче в дереве курса (например, "Программирование [Ассемблер i8086, C-MPA]\Простейшая (Программы с подсказками)\Сложение"). Возвращается только если в запросе был передан корректный courseId.
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

requests.get("https://dl.gsu.by/restapi/get-task-info",
    params={
      "nodeId": "1",
      "courseId": "0",
      "removeHtmlTags": "true"
    }
)

{
  "name": "string",
  "taskId": 1,
  "statement": "string",
  "path": null
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

    Расширение файла обязательно с точкой (например .pas, .cpp, .py, .java, можно любое), ищется в папке задачи по маске *sol* c выбранным расширением
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

requests.post("https://dl.gsu.by/restapi/get-solution",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "fileExtension": "",
      "taskId": 1
    }
)

{
  "solution": "string"
}

Успешный ответ с содержимым файла решения
Решения ​

Отправка решений на проверку и получение результатов
Решения Operations

    post/send-solution
    post/get-solution-result
    post/get-solutions

Отправить решение на проверку​

Сохраняет переданный код во временный файл и добавляет его в очередь на проверку. Возвращает Id в очереди (queueId).
Body
required
application/json

    code
    Type: string
    required

    Текст исходного кода решения
    fileExtension
    Type: string
    required

    Расширение файла обязательно с точкой (например, .pas, .cpp, .py)
    nodeId
    Type: integer
    required

    Id ноды, куда отправляется решение
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

Responses

    Type: object
        message
        Type: string

        Сообщение о результате отправки или текст ошибки
        queueId
        Type: integer

        Id отправленного решения в очереди тестирования (если 0, значит ошибка)
    application/json
    Type: object
        message
        Type: string

        Сообщение о результате отправки или текст ошибки
        queueId
        Type: integer

        Id отправленного решения в очереди тестирования (если 0, значит ошибка)
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    500

    Внутренняя ошибка сервера

Request Example for post/send-solution

requests.post("https://dl.gsu.by/restapi/send-solution",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "nodeId": 1,
      "code": "",
      "fileExtension": ".cpp"
    }
)

{
  "queueId": 1,
  "message": "string"
}

Успешный ответ (содержит queueId > 0)
Получить результат проверки решения​

Возвращает статус завершенности тестирования решения и его результат по переданному queueId. Если решение отсутствует в очереди тестирования и в логах, возвращается стандартный HTTP-ответ 404 Not Found.
Body
required
application/json

    queueId
    Type: integer
    required

    Id решения из очереди, возвращенный методом /send-solution
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

Responses

    Type: object
        comment
        Type: string

        Текстовый вердикт проверки (например, "Все тесты успешно пройдены", "не пройден 2-й тест (неверный ответ)" или пустая строка, если проверка еще не завершена)
        isFinished
        Type: boolean

        Флаг завершения проверки (false — в очереди или тестируется, true — проверка завершена)
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    404

    Решение с указанным queueId не найдено
    500

    Внутренняя ошибка сервера

Request Example for post/get-solution-result

requests.post(
    "https://dl.gsu.by/restapi/get-solution-result",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "queueId": 1
    }
)

{
  "isFinished": true,
  "comment": "string"
}

Успешный ответ со статусом и результатом тестирования
Получить список решений по задаче​

Возвращает список верных и ошибочных решений для указанной задачи, за выбранный период и с нужным расширением. Обычным пользователям возвращаются только их собственные решения, администраторам/редакторам курса - решения всех пользователей.
Body
required
application/json

    courseId
    Type: integer
    required

    Id курса, в контексте которого ищутся решения
    nodeId
    Type: integer
    required

    Id ноды (задачи)
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

    endDate
    Type: stringFormat: date-time

    Конец периода в формате 'YYYY-MM-DD' или 'YYYY-MM-DD HH:mm:ss' (необязательно)
    extension
    Type: string

    Расширение файла без точки (например, cpp, pas, java). Если не указано, вернутся решения с любыми расширениями.
    includeCorrect
    Type: boolean
    default:  
    true

    Включать ли в результат решения, прошедшие все тесты. По умолчанию true.
    includeIncorrect
    Type: boolean
    default:  
    true

    Включать ли в результат неверные решения (ошибки, не пройденные тесты). По умолчанию true.
    startDate
    Type: stringFormat: date-time

    Начало периода в формате 'YYYY-MM-DD' или 'YYYY-MM-DD HH:mm:ss' (необязательно)

Responses

    Type: object
        solutions
        Type: array object[]
            code
            Type: string

            Исходный код решения
            isCorrect
            Type: boolean

            Является ли решение полностью верным
            queueId
            Type: integer

            Id в очереди
            report
            Type: string

            Текст отчета тестирующей системы (вердикт)
            result
            Type: string

            Начисленные баллы за задачу
            userId
            Type: integer

            Id пользователя, отправившего решение
    application/json
    401

    Неавторизован
    500

    Внутренняя ошибка сервера

Request Example for post/get-solutions

requests.post("https://dl.gsu.by/restapi/get-solutions",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "courseId": 1,
      "nodeId": 1,
      "startDate": "",
      "endDate": "",
      "extension": "",
      "includeCorrect": True,
      "includeIncorrect": True
    }
)

{
  "solutions": [
    {
      "queueId": 1,
      "userId": 1,
      "code": "string",
      "result": "string",
      "report": "string",
      "isCorrect": true
    }
  ]
}

Успешный ответ со списком решений
Дерево задач ​

Навигация по дереву курсов и получение списков задач
Дерево задач Operations

    post/get-node-tasks
    post/get-node-tree
    post/get-course-node

Получить список задач внутри папки (без подпапок)​

Возвращает массив NodeID задач, которые находятся непосредственно внутри указанной папки. Задачи, к которым у пользователя нет доступа, отфильтровываются.
Body
required
application/json

    nodeId
    Type: integer
    required

    Id ноды (папки), для которой нужно получить список задач
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

    courseId
    Type: integer

    Id курса, в котором находится нода

Responses

    Type: object
        tasks
        Type: array object[]

        Список задач, к которым у пользователя есть доступ
            name
            Type: string

            Название задачи
            nodeId
            Type: integer

            Id ноды задачи
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    403

    Доступ к запрошенной папке запрещен
    404

    Папка с указанным NodeID не найдена
    500

    Внутренняя ошибка сервера

Request Example for post/get-node-tasks

requests.post("https://dl.gsu.by/restapi/get-node-tasks",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "courseId": 1,
      "nodeId": 1
    }
)

{
  "tasks": [
    {
      "nodeId": 1,
      "name": "string"
    }
  ]
}

Успешный ответ со списком NodeID задач
Получить список всех задач внутри папки (в виде вложенного дерева)​

Возвращает вложенное дерево (hierarchy) всех папок и задач, которые находятся внутри указанной папки. Недоступные пользователю ветки и задачи отфильтровываются.
Body
required
application/json

    nodeId
    Type: integer
    required

    Id ноды (папки), для которой нужно получить список задач
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

    courseId
    Type: integer

    Id курса, в котором находится нода

Responses

    Type: object
        tree
        Type: array object[]

        Дерево задач и папок
            children
            Type: array object[]

            Список вложенных нод (если это папка)
                children
                Type: array object[]

                Список вложенных нод (если это папка)
                    children
                    Type: array object[]

                    Список вложенных нод (если это папка)
                    isFolder
                    Type: boolean

                    Является ли эта нода папкой (true) или задачей (false)
                    name
                    Type: string

                    Название папки или задачи
                    nodeId
                    Type: integer

                    Id ноды
                isFolder
                Type: boolean

                Является ли эта нода папкой (true) или задачей (false)
                name
                Type: string

                Название папки или задачи
                nodeId
                Type: integer

                Id ноды
            isFolder
            Type: boolean

            Является ли эта нода папкой (true) или задачей (false)
            name
            Type: string

            Название папки или задачи
            nodeId
            Type: integer

            Id ноды
    application/json
    401

    Неавторизован — sessionId отсутствует или недействителен
    403

    Доступ к запрошенной корневой папке запрещен
    404

    Папка с указанным NodeID не найдена
    500

    Внутренняя ошибка сервера

Request Example for post/get-node-tree

requests.post("https://dl.gsu.by/restapi/get-node-tree",
    headers={
      "Content-Type": "application/json"
    },
    json={
      "sessionId": "",
      "courseId": 1,
      "nodeId": 1
    }
)

{
  "tree": [
    {
      "nodeId": 1,
      "name": "string",
      "isFolder": true,
      "children": [
        {
          "nodeId": 1,
          "name": "string",
          "isFolder": true,
          "children": [
            "[Circular Reference]"
          ]
        }
      ]
    }
  ]
}

Успешный ответ со структурой дерева
