Now implement the following:

1) A script to run the stack in docker
2) Implement authentication for backend, there will be 2 types of endpoints, both will have auths:
- endpoints for users 
-- register users (this is the only endpoints that will be public with no auth), input is a username string, check it in @word_scanner.py and against the list of usernames already in db (to be discuss later), if success, users auth will issue a uuid string that acts as the identifier from now on
-- request square location: functionalities to be explained later, this request will have:
    input {token: uuid, game_type: (either 1 or 2)}
    output {success: true/false, x: int, y: int, game_type: (either 1 or 2)}

-- submit result: functionalities to be explained later, this request will have:
    input {token: uuid, x:int, y:int, canvas: string (1024 characters), time_taken: float, game_type: (either 1 or 2)}
    output {success: true/false, x: int, y: int, game_type: (either 1 or 2)}

-- request_leaderboard: functionalities to be explained later, this request will have:
    input {token: uuid, game_type: (either 1 or 2)}
    output {success: true/false, List[{username: string, count: int, accuracy: float (0-100)}]  }

- endpoints for admins
admin auth is always going to use the same password (put in a secret file) AND the frontend is the admin dashboard, when the FE launched, it should ask for a username and password, check it against the secret file, if it match, return success, then FE should store those values and use them for every request from now on

the endpoints for admins will be expanded on later, but for now, one thing that definitely needs is that it should able to see/edit/add new rows/remove rows for the values of every tables in sqlite database, put it as a new tab in the FE

3) database schema:

all of the tables should contains: created_at (timestamp)

one table call requests that just logs every single requests that came in, what their inputs/outputs are, their ip address, headers (without uuid) and so on

users 
user_id (int increment) - username - uuid - registered_at

submitted_for_type_one
id - user_id - submitted_string (1024 chars) - accuracy
index on user_id

submitted_for_type_two
id - user_id - submitted_string (1024 chars) - accuracy
index on user_id