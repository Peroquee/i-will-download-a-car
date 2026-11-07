*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Input Page

*** Variables ***
${MAGNET}    magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

*** Test Cases ***
The YouTube Form Requires A Link Channel And Title
    Select Mode    youtube
    Submit The Form
    Assert Field Error    link-error    Please fill this in.
    Assert Field Error    f-channel-error    Please fill this in.
    Assert Field Error    f-yt-title-error    Please fill this in.
    Assert Field Invalid    link
    Assert Field Invalid    f-channel
    Assert Field Invalid    f-yt-title

An Invalid YouTube Address Is Refused
    Select Mode    youtube
    Input Text    id:link    https://www.youtube.com/@somechannel
    Input Text    id:f-channel    Some Channel
    Input Text    id:f-yt-title    Some Title
    Submit The Form
    Assert Field Error    link-error    This is not a link to a single YouTube video.
    Assert Field Invalid    link

The Magnet Form Requires Its Fields
    Select Mode    magnet
    Submit The Form
    Assert Field Error    link-error    Please fill this in.
    Assert Field Error    f-movie-title-error    Please fill this in.
    Assert Field Error    f-year-error    Please fill this in.

An Invalid Magnet Link Is Refused
    Select Mode    magnet
    Input Text    id:link    magnet:?xt=urn:btih:not-a-valid-hash
    Input Text    id:f-movie-title    A Film
    Input Text    id:f-year    2000
    Submit The Form
    Assert Field Error    link-error    This is not a valid magnet link.
    Assert Field Invalid    link

A Year Outside The Range Is Refused
    Select Mode    magnet
    Input Text    id:link    ${MAGNET}
    Input Text    id:f-movie-title    A Film
    Input Text    id:f-year    1066
    Submit The Form
    Assert Field Error    f-year-error    Year between 1870
    Assert Field Invalid    f-year

A Season Must Be A Whole Number
    Select Mode    magnet
    Select From List By Value    id:media-type    series
    Input Text    id:link    ${MAGNET}
    Input Text    id:f-series-title    A Show
    Input Text    id:f-season    x
    Submit The Form
    Assert Field Error    f-season-error    Please enter a whole number.
    Assert Field Invalid    f-season

The Podcast Form Requires A Folder And A Choice
    Select Mode    podcast
    Submit The Form
    Assert Field Error    link-error    Please fill this in.
    Assert Field Error    f-podcast-error    Please fill this in.
    Assert Field Error    pc-episode-error    Please choose an episode or subscribe to the podcast.

A Non Https Feed Address Is Refused
    Select Mode    podcast
    Input Text    id:link    ftp://example.org/feed
    Input Text    id:f-podcast    A Podcast
    Select Checkbox    id:pc-subscribe
    Submit The Form
    Assert Field Error    link-error    not a valid feed address

Fixing The Link And Resubmitting Clears Its Error
    Select Mode    magnet
    Submit The Form
    Assert Field Error    link-error    Please fill this in.
    Input Text    id:link    ${MAGNET}
    Submit The Form
    Element Should Not Be Visible    id:link-error
    Assert Field Valid    link

A Valid YouTube Link Is Looked Up And Queued
    Select Mode    youtube
    Input Text    id:link    https://www.youtube.com/watch?v=aqz-KE-bpKQ
    Wait Until Keyword Succeeds    20s    0.5s    Textfield Value Should Be    id:f-yt-title    Big Buck Bunny
    Textfield Value Should Be    id:f-channel    Blender Foundation
    Submit The Form
    Wait For Job Result

A Valid Magnet Link Becomes A Job
    Select Mode    magnet
    Input Text    id:link    ${MAGNET}
    Input Text    id:f-movie-title    A Film
    Input Text    id:f-year    2000
    Submit The Form
    Wait For Job Result
