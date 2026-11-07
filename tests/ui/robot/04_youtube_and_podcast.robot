*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Input Page

*** Test Cases ***
The YouTube Target Changes The Fields
    Select Mode    youtube
    Select From List By Value    id:yt-target    podcast
    Wait Until Element Is Visible    id:yt-audio-row
    Element Should Contain    id:yt-channel-label    Podcast
    Element Should Contain    id:yt-title-label    Episode
    Select From List By Value    id:yt-target    music
    Wait Until Element Is Visible    id:yt-album-row
    Element Should Contain    id:yt-channel-label    Artist
    Element Should Contain    id:yt-title-label    Track title

A YouTube Video Can Subscribe To Its Channel
    Select Mode    youtube
    Input Text    id:link    https://www.youtube.com/watch?v=aqz-KE-bpKQ
    Wait Until Keyword Succeeds    20s    0.5s    Textfield Value Should Be    id:f-yt-title    Big Buck Bunny
    Select Checkbox    id:subscribe
    Wait Until Element Is Visible    id:yt-backfill-row
    Submit The Form
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:result    Subscribed to the channel

A Podcast Feed Can Queue An Episode
    Select Mode    podcast
    Input Text    id:link    https://example.org/feed.xml
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:pc-status    Found
    Wait Until Keyword Succeeds    20s    0.5s    Textfield Value Should Be    id:f-podcast    Seeded Podcast
    Select From List By Index    id:pc-episode    1
    Submit The Form
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:result    Episode queued as job

A Podcast Feed Can Be Subscribed
    Select Mode    podcast
    Input Text    id:link    https://example.org/subscribe.xml
    Wait Until Keyword Succeeds    20s    0.5s    Textfield Value Should Be    id:f-podcast    Seeded Podcast
    Select Checkbox    id:pc-subscribe
    Submit The Form
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:result    Subscribed to the podcast
