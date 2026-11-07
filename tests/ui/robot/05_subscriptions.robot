*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Subscriptions Page

*** Test Cases ***
The Subscriptions Are Grouped And Show Their Followers
    Wait Until Element Is Visible    id:subs-youtube-group
    Element Should Contain    id:subs-youtube-list    Seeded Channel
    Element Should Contain    id:subs-youtube-list    Blender Foundation
    Element Should Contain    id:subs-youtube-list    Followed by:
    Element Should Contain    id:subs-youtube-list    alice
    Wait Until Element Is Visible    id:subs-podcast-group
    Element Should Contain    id:subs-podcast-list    Seeded Podcast

Checking A Podcast Now Is Reported In Its Row
    ${row}=    Set Variable    xpath=(//li[contains(@class,'job')][contains(.,'Seeded Podcast')])[1]
    Wait Until Element Is Visible    ${row}
    Click Button    ${row}//button[normalize-space(.)='Check now']
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    ${row}    Checked:

Removing A Subscription Takes Two Clicks
    ${rows}=    Get Element Count    xpath=//li[contains(@class,'job')][contains(.,'Seeded Podcast')]
    Should Be True    ${rows} >= 1
    ${row}=    Set Variable    xpath=(//li[contains(@class,'job')][contains(.,'Seeded Podcast')])[1]
    Click Button    ${row}//button[normalize-space(.)='Remove']
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Really remove?']
    Click Button    ${row}//button[normalize-space(.)='Really remove?']
    Wait Until Keyword Succeeds    20s    0.5s    Subscription Rows Decrease    ${rows}

Removing For Everyone Takes Two Clicks
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'Seeded Channel')]
    Wait Until Element Is Visible    ${row}
    Click Button    ${row}//button[normalize-space(.)='Remove for everyone']
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Really remove for everyone?']
    Click Button    ${row}//button[normalize-space(.)='Really remove for everyone?']
    Wait Until Keyword Succeeds    20s    0.5s    Page Should Not Contain    Seeded Channel
