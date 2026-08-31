import { useNavigate } from 'react-router-dom'
import LandingView from './LandingView'

export default function Landing() {
    const navigate = useNavigate()

    const handleLoginClick = () => {
        navigate('/login')
    }

    return (
        <LandingView onLoginClick={handleLoginClick} />
    )
}