import jaseci.jsorc.live_actions as jla


@jla.jaseci_action(act_group=["demo_test1"])
def action1(message: str):
    return f"Action1: {message}"
