active_project(Project) :-
    project(Project),
    active(Project).

important_task(Task) :-
    task(Task),
    belongs_to(Task, Project),
    active_project(Project).

high_priority(Task) :-
    important_task(Task),
    deadline_soon(Task).

high_priority(Task) :-
    important_task(Task),
    depends_on(Task, _Dependency).

high_priority_reason(Task, belongs_to_active_project) :-
    important_task(Task).

high_priority_reason(Task, deadline_soon) :-
    high_priority(Task),
    deadline_soon(Task).

high_priority_reason(Task, important_task) :-
    high_priority(Task),
    important_task(Task).

high_priority_reason(Task, dependency) :-
    high_priority(Task),
    depends_on(Task, _Dependency).

blocked_task(Task) :-
    task(Task),
    blocked(Task).

overdue_task(Task) :-
    task(Task),
    overdue(Task).

dependency_chain(Task, Dependency) :-
    depends_on(Task, Dependency).

dependency_chain(Task, Dependency) :-
    depends_on(Task, Intermediate),
    dependency_chain(Intermediate, Dependency).

current_project_focus(Project) :-
    project(Project),
    active(Project),
    focused(Project).

current_project_focus(Project) :-
    project(Project),
    active(Project),
    \+ focused(_).

morning_priority(Task) :-
    high_priority(Task).

morning_priority(Task) :-
    overdue_task(Task).

morning_priority_reason(Task, overdue) :-
    morning_priority(Task),
    overdue(Task).

morning_priority_reason(Task, blocked) :-
    morning_priority(Task),
    blocked(Task).

morning_priority_reason(Task, high_priority) :-
    morning_priority(Task),
    high_priority(Task).
